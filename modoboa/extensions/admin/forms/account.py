from django import forms
from django.utils.translation import ugettext as _, ugettext_lazy
from django.http import QueryDict
from modoboa.lib import events, parameters
from modoboa.lib.exceptions import PermDeniedException
from modoboa.lib.permissions import get_account_roles
from modoboa.lib.emailutils import split_mailbox
from modoboa.lib.formutils import (
    DomainNameField, DynamicForm, TabForms
)
from modoboa.core.models import User
from modoboa.extensions.admin.exceptions import AdminError
from modoboa.extensions.admin.models import (
    Domain, Mailbox, Alias
)


class AccountFormGeneral(forms.ModelForm):
    username = forms.CharField(label=ugettext_lazy("Username"))
    role = forms.ChoiceField(
        label=ugettext_lazy("Role"),
        choices=[('', ugettext_lazy("Choose"))],
        help_text=ugettext_lazy("What level of permission this user will have")
    )
    password1 = forms.CharField(label=_("Password"), widget=forms.PasswordInput)
    password2 = forms.CharField(
        label=ugettext_lazy("Confirmation"),
        widget=forms.PasswordInput,
        help_text=ugettext_lazy("Enter the same password as above, for verification.")
    )

    class Meta:
        model = User
        fields = ("username", "first_name", "last_name", "role", "is_active")

    def __init__(self, user, *args, **kwargs):
        super(AccountFormGeneral, self).__init__(*args, **kwargs)
        self.fields.keyOrder = ['role', 'username', 'first_name', 'last_name',
                                'password1', 'password2', 'is_active']
        self.fields["is_active"].label = _("Enabled")
        self.user = user
        if user.group == "DomainAdmins":
            self.fields["role"] = forms.CharField(
                label="",
                widget=forms.HiddenInput, required=False
            )
        else:
            self.fields["role"].choices = \
                [('', ugettext_lazy("Choose"))] + get_account_roles(user)

        if "instance" in kwargs:
            if len(args) \
               and (args[0].get("password1", "") == ""
               and args[0].get("password2", "") == ""):
                self.fields["password1"].required = False
                self.fields["password2"].required = False

            u = kwargs["instance"]
            self.fields["role"].initial = u.group

            if not u.is_local \
               and parameters.get_admin("LDAP_AUTH_METHOD") == "directbind":
                del self.fields["password1"]
                del self.fields["password2"]

    def clean_role(self):
        if self.user.group == "DomainAdmins":
            if self.instance == self.user:
                return "DomainAdmins"
            return "SimpleUsers"
        return self.cleaned_data["role"]

    def clean_username(self):
        from django.core.validators import validate_email
        if not "role" in self.cleaned_data:
            return self.cleaned_data["username"]
        if self.cleaned_data["role"] != "SimpleUsers":
            return self.cleaned_data["username"]
        uname = self.cleaned_data["username"].lower()
        validate_email(uname)
        return uname

    def clean_password2(self):
        password1 = self.cleaned_data.get("password1", "")
        password2 = self.cleaned_data["password2"]
        if password1 != password2:
            raise forms.ValidationError(_("The two password fields didn't match."))
        return password2

    def save(self, commit=True):
        account = super(AccountFormGeneral, self).save(commit=False)
        if self.user == account and not self.cleaned_data["is_active"]:
            raise AdminError(_("You can't disable your own account"))
        if commit:
            if "password1" in self.cleaned_data \
               and self.cleaned_data["password1"] != "":
                account.set_password(self.cleaned_data["password1"])
            account.save()
            account.set_role(self.cleaned_data["role"])
        return account


class AccountFormMail(forms.Form, DynamicForm):
    email = forms.EmailField(label=ugettext_lazy("E-mail"), required=False)
    quota = forms.IntegerField(
        label=ugettext_lazy("Quota"),
        required=False,
        help_text=_("Quota in MB for this mailbox. Define a custom value or use domain's default one. Leave empty to define an unlimited value (not allowed for domain administrators)."),
        widget=forms.widgets.TextInput(attrs={"class": "span1"})
    )
    quota_act = forms.BooleanField(required=False)
    aliases = forms.EmailField(
        label=ugettext_lazy("Alias(es)"),
        required=False,
        help_text=ugettext_lazy("Alias(es) of this mailbox. Indicate only one address per input, press ENTER to add a new input. Use the '*' character to create a 'catchall' alias (ex: *@domain.tld).")
    )

    def __init__(self, *args, **kwargs):
        if "instance" in kwargs:
            self.mb = kwargs["instance"]
            del kwargs["instance"]
        super(AccountFormMail, self).__init__(*args, **kwargs)
        if hasattr(self, "mb") and self.mb is not None:
            self.fields["email"].required = True
            cpt = 1
            for alias in self.mb.alias_set.all():
                if len(alias.get_recipients()) >= 2:
                    continue
                name = "aliases_%d" % cpt
                self._create_field(forms.EmailField, name, alias.full_address)
                cpt += 1
            self.fields["email"].initial = self.mb.full_address            
            self.fields["quota_act"].initial = self.mb.use_domain_quota
            if not self.mb.use_domain_quota and self.mb.quota:
                self.fields["quota"].initial = self.mb.quota
        else:
            self.fields["quota_act"].initial = True

        if len(args) and isinstance(args[0], QueryDict):
            self._load_from_qdict(args[0], "aliases", forms.EmailField)

    def clean_email(self):
        """Ensure lower case emails"""
        return self.cleaned_data["email"].lower()

    def save(self, user, account):
        if self.cleaned_data["email"] == "":
            return None

        if self.cleaned_data["quota_act"]:
            self.cleaned_data["quota"] = None

        if not hasattr(self, "mb") or self.mb is None:
            locpart, domname = split_mailbox(self.cleaned_data["email"])
            try:
                domain = Domain.objects.get(name=domname)
            except Domain.DoesNotExist:
                raise AdminError(_("Domain does not exist"))
            if not user.can_access(domain):
                raise PermDeniedException
            try:
                Mailbox.objects.get(address=locpart, domain=domain)
            except Mailbox.DoesNotExist:
                pass
            else:
                raise AdminError(_("Mailbox %s already exists" % self.cleaned_data["email"]))
            events.raiseEvent("CanCreate", user, "mailboxes")
            self.mb = Mailbox(address=locpart, domain=domain, user=account,
                              use_domain_quota=self.cleaned_data["quota_act"])
            self.mb.set_quota(self.cleaned_data["quota"], 
                              user.has_perm("admin.add_domain"))
            self.mb.save(creator=user)
        else:
            newaddress = None
            if self.cleaned_data["email"] != self.mb.full_address:
                newaddress = self.cleaned_data["email"]
            elif account.group == "SimpleUsers" and account.username != self.mb.full_address:
                newaddress = account.username
            if newaddress is not None:
                local_part, domname = split_mailbox(newaddress)
                try:
                    domain = Domain.objects.get(name=domname)
                except Domain.DoesNotExist:
                    raise AdminError(_("Domain does not exist"))
                if not user.can_access(domain):
                    raise PermDeniedException
                self.mb.rename(local_part, domain)

            self.mb.use_domain_quota = self.cleaned_data["quota_act"]
            override_rules = True \
                if not self.mb.quota or user.has_perm("admin.add_domain") \
                else False
            self.mb.set_quota(self.cleaned_data["quota"], override_rules)
            self.mb.save()

        account.email = self.cleaned_data["email"]
        account.save()

        for name, value in self.cleaned_data.iteritems():
            if not name.startswith("aliases"):
                continue
            if value == "":
                continue
            local_part, domname = split_mailbox(value)
            try:
                self.mb.alias_set.get(address=local_part, domain__name=domname)
            except Alias.DoesNotExist:
                pass
            else:
                continue
            events.raiseEvent("CanCreate", user, "mailbox_aliases")
            al = Alias(address=local_part, enabled=account.is_active)
            al.domain = Domain.objects.get(name=domname)
            al.save(int_rcpts=[self.mb], creator=user)

        for alias in self.mb.alias_set.all():
            if len(alias.get_recipients()) >= 2:
                continue
            if not len(filter(lambda name: self.cleaned_data[name] == alias.full_address,
                              self.cleaned_data.keys())):
                alias.delete()

        return self.mb


class AccountPermissionsForm(forms.Form, DynamicForm):
    domains = DomainNameField(
        label=ugettext_lazy("Domain(s)"),
        required=False,
        help_text=ugettext_lazy("Domain(s) that user administrates")
    )

    def __init__(self, *args, **kwargs):
        if "instance" in kwargs:
            self.account = kwargs["instance"]
            del kwargs["instance"]

        super(AccountPermissionsForm, self).__init__(*args, **kwargs)

        if not hasattr(self, "account") or self.account is None:
            return
        for pos, domain in enumerate(Domain.objects.get_for_admin(self.account)):
            name = "domains_%d" % (pos + 1)
            self._create_field(DomainNameField, name, domain.name)
        if len(args) and isinstance(args[0], QueryDict):
            self._load_from_qdict(args[0], "domains", DomainNameField)

    def save(self):
        current_domains = [dom.name for dom in Domain.objects.get_for_admin(self.account)]
        for name, value in self.cleaned_data.items():
            if not name.startswith("domains"):
                continue
            if value in ["", None]:
                continue
            if not value in current_domains:
                domain = Domain.objects.get(name=value)
                domain.add_admin(self.account)

        for domain in Domain.objects.get_for_admin(self.account):
            if not len(filter(lambda name: self.cleaned_data[name] == domain.name,
                              self.cleaned_data.keys())):
                domain.remove_admin(self.account)


class AccountForm(TabForms):

    def __init__(self, user, *args, **kwargs):
        self.user = user
        self.forms = [
            dict(id="general", title=_("General"), cls=AccountFormGeneral,
                 new_args=[user], mandatory=True),
            dict(id="mail", title=_("Mail"), formtpl="admin/mailform.html",
                 cls=AccountFormMail),
            dict(id="perms", title=_("Permissions"), formtpl="admin/permsform.html",
                 cls=AccountPermissionsForm)
        ]
        cbargs = [user]
        if "instances" in kwargs:
            cbargs += [kwargs["instances"]["general"]]
        self.forms += events.raiseQueryEvent("ExtraAccountForm", *cbargs)

        super(AccountForm, self).__init__(*args, **kwargs)

    def check_perms(self, account):
        if account.is_superuser:
            return False
        return self.user.has_perm("admin.add_domain") \
            and account.has_perm("core.add_user")

    def _before_is_valid(self, form):
        if form["id"] == "general":
            return True

        if hasattr(self, "check_%s" % form["id"]):
            if not getattr(self, "check_%s" % form["id"])(self.account):
                return False
            return True

        if False in events.raiseQueryEvent("CheckExtraAccountForm", self.account, form):
            return False
        return True

    def save_general_form(self):
        self.account = self.forms[0]["instance"].save()

    def save(self):
        """Custom save method

        As forms interact with each other, it is simpler to make
        custom code to save them.
        """
        self.forms[1]["instance"].save(self.user, self.account)
        if len(self.forms) <= 2:
            return
        for f in self.forms[2:]:
            f["instance"].save()
