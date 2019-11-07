# Copyright 2012-2017 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Custom model and form fields."""

__all__ = [
    "CIDRField",
    "EditableBinaryField",
    "Field",
    "HostListFormField",
    "IPListFormField",
    "IPv4CIDRField",
    "MAASIPAddressField",
    "MAC",
    "MACAddressField",
    "MACAddressFormField",
    "MODEL_NAME_VALIDATOR",
    "NodeChoiceField",
    "register_mac_type",
    "VerboseRegexValidator",
    "VersionedTextFileField",
    "validate_mac",
]

from copy import deepcopy
from json import dumps, loads
import re

from django import forms
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.validators import RegexValidator, URLValidator
from django.db import connections
from django.db.models import (
    BinaryField,
    CharField,
    Field as _BrokenField,
    GenericIPAddressField,
    IntegerField,
    Q,
    URLField,
)
from django.utils.deconstruct import deconstructible
from django.utils.encoding import force_text
from django.utils.translation import ugettext_lazy as _
from maasserver.models.versionedtextfile import VersionedTextFile
from maasserver.utils.dns import validate_domain_name, validate_hostname
from maasserver.utils.orm import get_one, validate_in_transaction
from netaddr import AddrFormatError, IPAddress, IPNetwork
from provisioningserver.utils import typed
import psycopg2.extensions

# Validator for the name attribute of model entities.
MODEL_NAME_VALIDATOR = RegexValidator(r"^\w[ \w-]*$")


class Field(_BrokenField):
    """Django's `Field` has a mutable default argument, hence is broken.

    This fixes it.
    """

    def __init__(self, *args, validators=None, **kwargs):
        kwargs["validators"] = [] if validators is None else validators
        super(Field, self).__init__(*args, **kwargs)


MAC_RE = re.compile(
    r"^\s*("
    r"([0-9a-fA-F]{1,2}:){5}[0-9a-fA-F]{1,2}|"
    r"([0-9a-fA-F]{1,2}-){5}[0-9a-fA-F]{1,2}|"
    r"([0-9a-fA-F]{3,4}.){2}[0-9a-fA-F]{3,4}"
    r")\s*$"
)

MAC_ERROR_MSG = "'%(value)s' is not a valid MAC address."


class VerboseRegexValidator(RegexValidator):
    """A verbose `RegexValidator`.

    This `RegexValidator` includes the checked value in the rendered error
    message when the validation fails.
    """

    # Set a bugus code to circumvent Django's attempt to re-interpret a
    # validator's error message using the field's message it is attached
    # to.
    code = "bogus-code"

    def __call__(self, value):
        """Validates that the input matches the regular expression."""
        if not self.regex.search(force_text(value)):
            raise ValidationError(
                self.message % {"value": value}, code=self.code
            )


mac_validator = VerboseRegexValidator(regex=MAC_RE, message=MAC_ERROR_MSG)


def validate_mac(value):
    """Django validator for a MAC."""
    if isinstance(value, MAC):
        value = value.get_raw()
    mac_validator(value)


class StrippedCharField(forms.CharField):
    """A CharField that will strip surrounding whitespace before validation."""

    def clean(self, value):
        value = self.to_python(value).strip()
        return super(StrippedCharField, self).clean(value)


class UnstrippedCharField(forms.CharField):
    """A version of forms.CharField that never strips the whitespace.

    Django 1.9 has introduced a strip argument that controls stripping of
    whitespace *and* which defaults to True, thus breaking compatibility with
    1.8 and earlier.
    """

    def __init__(self, *args, **kwargs):
        # Instead of relying on a version check, we check for CharField
        # constructor having a strip kwarg instead.
        parent_init = super(UnstrippedCharField, self).__init__
        if "strip" in parent_init.__code__.co_varnames:
            parent_init(*args, strip=False, **kwargs)
        else:
            # In Django versions that do not support strip, False was the
            # default.
            parent_init(*args, **kwargs)


class VerboseRegexField(forms.CharField):
    def __init__(self, regex, message, *args, **kwargs):
        """A field that validates its value with a regular expression.

        :param regex: Either a string or a compiled regular expression object.
        :param message: Error message to use when the validation fails.
        """
        super(VerboseRegexField, self).__init__(*args, **kwargs)
        self.validators.append(
            VerboseRegexValidator(regex=regex, message=message)
        )


class MACAddressFormField(VerboseRegexField):
    """Form field type: MAC address."""

    def __init__(self, *args, **kwargs):
        super(MACAddressFormField, self).__init__(
            regex=MAC_RE, message=MAC_ERROR_MSG, *args, **kwargs
        )


class MACAddressField(Field):
    """Model field type: MAC address."""

    description = "MAC address"

    default_validators = [validate_mac]

    def db_type(self, *args, **kwargs):
        return "macaddr"

    def to_python(self, value):
        return MAC(value)

    def from_db_value(self, value, expression, connection, context):
        return MAC(value)

    def get_prep_value(self, value):
        value = super(MACAddressField, self).get_prep_value(value)
        # Convert empty string to None.
        if not value:
            return None
        return value


class MAC:
    """A MAC address represented as a database value.

    PostgreSQL supports MAC addresses as a native type. They show up
    client-side as this class. It is essentially a wrapper for a string.

    This NEVER represents a null or empty MAC address.
    """

    def __new__(cls, value):
        """Return `None` if `value` is `None` or the empty string."""
        if value is None:
            return None
        elif isinstance(value, (bytes, str)):
            return None if len(value) == 0 else super(MAC, cls).__new__(cls)
        else:
            return super(MAC, cls).__new__(cls)

    def __init__(self, value):
        """Wrap a MAC address, or None, into a `MAC`.

        :param value: A MAC address, in the form of a string or a `MAC`;
            or None.
        """
        # The wrapped attribute is stored as self._wrapped, following
        # ISQLQuote's example.
        if isinstance(value, MAC):
            self._wrapped = value._wrapped
        elif isinstance(value, bytes):
            self._wrapped = value.decode("ascii")
        elif isinstance(value, str):
            self._wrapped = value
        else:
            raise TypeError("expected MAC or string, got: %r" % (value,))

    def __conform__(self, protocol):
        """Tell psycopg2 that this type implements the adapter protocol."""
        # The psychopg2 docs say to check that the protocol is ISQLQuote,
        # but not what to do if it isn't.
        assert protocol == psycopg2.extensions.ISQLQuote, (
            "Unsupported psycopg2 adapter protocol: %s" % protocol
        )
        return self

    def getquoted(self):
        """Render this object in SQL.

        This is part of psycopg2's adapter protocol.
        """
        return "'%s'::macaddr" % self._wrapped

    def get_raw(self):
        """Return the wrapped value."""
        return self._wrapped

    @property
    def raw(self):
        """The MAC address as a string."""
        return self._wrapped

    @classmethod
    def parse(cls, value, cur):
        """Turn a value as received from the database into a MAC."""
        return cls(value)

    def __repr__(self):
        """Represent the MAC as a string."""
        return "<MAC %s>" % self._wrapped

    def __str__(self):
        """Represent the MAC as a Unicode string."""
        return self._wrapped

    def __bytes__(self):
        return self._wrapped.encode("ascii")

    def __eq__(self, other):
        """Two `MAC`s are equal if they wrap the same value.

        A MAC is is also equal to the value it wraps. This is non-commutative,
        but it supports Django code that compares input values to various
        kinds of "null" or "empty."
        """
        if isinstance(other, MAC):
            return self._wrapped == other._wrapped
        else:
            return self._wrapped == other

    def __ne__(self, other):
        return not (self == other)

    def __hash__(self):
        return hash(self._wrapped)


def register_mac_type(cursor):
    """Register our `MAC` type with psycopg2 and Django."""

    # This is standard, but not built-in, magic to register a type in
    # psycopg2: execute a query that returns a field of the corresponding
    # database type, then get its oid out of the cursor, use that to create
    # a "typecaster" in psycopg (by calling new_type(), confusingly!), then
    # register that type in psycopg.
    cursor.execute("SELECT NULL::macaddr")
    oid = cursor.description[0][1]
    mac_caster = psycopg2.extensions.new_type((oid,), "macaddr", MAC.parse)
    psycopg2.extensions.register_type(mac_caster)

    # Now do the same for the type array-of-MACs.  The "typecaster" created
    # for MAC is passed in; it gets used for parsing an individual element
    # of an array's text representation as received from the database.
    cursor.execute("SELECT '{}'::macaddr[]")
    oid = cursor.description[0][1]
    psycopg2.extensions.register_type(
        psycopg2.extensions.new_array_type((oid,), "macaddr", mac_caster)
    )


class JSONObjectField(Field):
    """A field that will store any jsonizable python object."""

    def to_python(self, value):
        """db -> python: json load."""
        assert not isinstance(value, bytes)
        if value is not None:
            if isinstance(value, str):
                try:
                    return loads(value)
                except ValueError:
                    pass
            return value
        else:
            return None

    def from_db_value(self, value, expression, connection, context):
        return self.to_python(value)

    def get_db_prep_value(self, value, connection=None, prepared=False):
        """python -> db: json dump.

        Keys are sorted when dumped to guarantee stable output. DB field can
        guarantee uniqueness and be queried (the same dict makes the same
        JSON).
        """
        if value is not None:
            return dumps(deepcopy(value), sort_keys=True)
        else:
            return None

    def get_internal_type(self):
        return "TextField"

    def formfield(self, form_class=None, **kwargs):
        """Return a plain `forms.Field` here to avoid "helpful" conversions.

        Django's base model field defaults to returning a `CharField`, which
        means that anything that's not character data gets smooshed to text by
        `CharField.to_pytnon` in forms (via the woefully named `smart_text`).
        This is not helpful.
        """
        if form_class is None:
            form_class = forms.Field
        return super().formfield(form_class=form_class, **kwargs)


class XMLField(Field):
    """A field for storing xml natively.

    This is not like the removed Django XMLField which just added basic python
    level checking on top of a text column.

    Really inserts should be wrapped like `XMLPARSE(DOCUMENT value)` but it's
    hard to do from django so rely on postgres supporting casting from char.
    """

    description = "XML document or fragment"

    def db_type(self, connection):
        return "xml"


class EditableBinaryField(BinaryField):
    """An editable binary field.

    An editable version of Django's BinaryField.
    """

    def __init__(self, *args, **kwargs):
        super(EditableBinaryField, self).__init__(*args, **kwargs)
        self.editable = True

    def deconstruct(self):
        # Override deconstruct not to fail on the removal of the 'editable'
        # field: the Django migration module assumes the field has its default
        # value (False).
        return Field.deconstruct(self)


class MAASIPAddressField(GenericIPAddressField):
    """A version of GenericIPAddressField with a custom get_internal_type().

    This class exists to work around a bug in Django that inserts a HOST() cast
    on the IP, causing the wrong comparison on the IP field.  See
    https://code.djangoproject.com/ticket/11442 for details.
    """

    def get_internal_type(self):
        """Returns a value different from 'GenericIPAddressField' and
        'IPAddressField' to force Django not to use a HOST() case when
        performing operation on this field.
        """
        return "IPField"

    def db_type(self, connection):
        """Returns the database column data type for IPAddressField.

        Override the default implementation which uses get_internal_type()
        and force a 'inet' type field.
        """
        return "inet"


class LargeObjectFile:
    """Large object file.

    Proxy the access from this object to psycopg2.
    """

    def __init__(self, oid=0, field=None, instance=None, block_size=(1 << 16)):
        self.oid = oid
        self.field = field
        self.instance = instance
        self.block_size = block_size
        self._lobject = None

    def __getattr__(self, name):
        if self._lobject is None:
            raise IOError("LargeObjectFile is not opened.")
        return getattr(self._lobject, name)

    def __enter__(self, *args, **kwargs):
        return self

    def __exit__(self, *args, **kwargs):
        self.close()

    def __iter__(self):
        return self

    @typed
    def write(self, data: bytes):
        """Write `data` to the underlying large object.

        This exists so that type annotations can be enforced.
        """
        self._lobject.write(data)

    def open(
        self, mode="rwb", new_file=None, using="default", connection=None
    ):
        """Opens the internal large object instance."""
        if "b" not in mode:
            raise ValueError("Large objects must be opened in binary mode.")
        if connection is None:
            connection = connections[using]
        validate_in_transaction(connection)
        self._lobject = connection.connection.lobject(
            self.oid, mode, 0, new_file
        )
        self.oid = self._lobject.oid
        return self

    def unlink(self):
        """Removes the large object."""
        if self._lobject is None:
            # Need to open the lobject so we get a reference to it in the
            # database, to perform the unlink.
            self.open()
            self.close()
        self._lobject.unlink()
        self._lobject = None
        self.oid = 0

    def __next__(self):
        r = self.read(self.block_size)
        if len(r) == 0:
            raise StopIteration
        return r


class LargeObjectDescriptor(object):
    """LargeObjectField descriptor."""

    def __init__(self, field):
        self.field = field

    def __get__(self, instance, type=None):
        if instance is None:
            return self
        return instance.__dict__[self.field.name]

    def __set__(self, instance, value):
        value = self.field.to_python(value)
        if value is not None:
            if not isinstance(value, LargeObjectFile):
                value = LargeObjectFile(value, self.field, instance)
        instance.__dict__[self.field.name] = value


class LargeObjectField(IntegerField):
    """A field that stores large amounts of data into postgres large object
    storage.

    Internally the field on the model is an `oid` field, that returns a proxy
    to the referenced large object.
    """

    def __init__(self, *args, **kwargs):
        self.block_size = kwargs.pop("block_size", 1 << 16)
        super(LargeObjectField, self).__init__(*args, **kwargs)

    @property
    def validators(self):
        # No validation. IntegerField will add incorrect validation. This
        # removes that validation.
        return []

    def db_type(self, connection):
        """Returns the database column data type for LargeObjectField."""
        # oid is the column type postgres uses to reference a large object
        return "oid"

    def contribute_to_class(self, cls, name):
        """Set the descriptor for the large object."""
        super(LargeObjectField, self).contribute_to_class(cls, name)
        setattr(cls, self.name, LargeObjectDescriptor(self))

    def get_db_prep_value(self, value, connection=None, prepared=False):
        """python -> db: `oid` value"""
        if value is None:
            return None
        if isinstance(value, LargeObjectFile):
            if value.oid > 0:
                return value.oid
            raise AssertionError(
                "LargeObjectFile's oid must be greater than 0."
            )
        raise AssertionError(
            "Invalid LargeObjectField value (expected LargeObjectFile): '%s'"
            % repr(value)
        )

    def to_python(self, value):
        """db -> python: `LargeObjectFile`"""
        if value is None:
            return None
        elif isinstance(value, LargeObjectFile):
            return value
        elif isinstance(value, int):
            return LargeObjectFile(value, self, self.model, self.block_size)
        raise AssertionError(
            "Invalid LargeObjectField value (expected integer): '%s'"
            % repr(value)
        )


class CIDRField(Field):
    description = "PostgreSQL CIDR field"

    def parse_cidr(self, value):
        try:
            return str(IPNetwork(value).cidr)
        except AddrFormatError as e:
            raise ValidationError(str(e)) from e

    def db_type(self, connection):
        return "cidr"

    def get_prep_value(self, value):
        if value is None or value == "":
            return None
        return self.parse_cidr(value)

    def from_db_value(self, value, expression, connection, context):
        if value is None:
            return value
        return self.parse_cidr(value)

    def to_python(self, value):
        if value is None or value == "":
            return None
        if isinstance(value, IPNetwork):
            return str(value)
        if not value:
            return value
        return self.parse_cidr(value)

    def formfield(self, **kwargs):
        defaults = {"form_class": forms.CharField}
        defaults.update(kwargs)
        return super(CIDRField, self).formfield(**defaults)


class IPv4CIDRField(CIDRField):
    """IPv4-only CIDR"""

    def get_prep_value(self, value):
        if value is None or value == "":
            return None
        return self.to_python(value)

    def to_python(self, value):
        if value is None or value == "":
            return None
        else:
            try:
                cidr = IPNetwork(value)
            except AddrFormatError:
                raise ValidationError(
                    "Invalid network: %(cidr)s", params={"cidr": value}
                )
            if cidr.cidr.version != 4:
                raise ValidationError(
                    "%(cidr)s: Only IPv4 networks supported.",
                    params={"cidr": value},
                )
        return str(cidr.cidr)


class IPListFormField(forms.CharField):
    """Accepts a space/comma separated list of IP addresses.

    This field normalizes the list to a space-separated list.
    """

    separators = re.compile(r"[,\s]+")

    def clean(self, value):
        if value is None:
            return None
        else:
            ips = re.split(self.separators, value)
            ips = [ip.strip() for ip in ips if ip != ""]
            for ip in ips:
                try:
                    GenericIPAddressField().clean(ip, model_instance=None)
                except ValidationError:
                    raise ValidationError(
                        "Invalid IP address: %s; provide a list of "
                        "space-separated IP addresses" % ip
                    )
            return " ".join(ips)


class HostListFormField(forms.CharField):
    """Accepts a space/comma separated list of hostnames or IP addresses.

    This field normalizes the list to a space-separated list.
    """

    separators = re.compile(r"[,\s]+")

    # Regular expressions to sniff out things that look like IP addresses;
    # additional and more robust validation ought to be done to make sure.
    pt_ipv4 = r"(?: \d{1,3} [.] \d{1,3} [.] \d{1,3} [.] \d{1,3} )"
    pt_ipv6 = r"(?: (?: [\da-fA-F]+ :+)+ (?: [\da-fA-F]+ | %s )+ )" % pt_ipv4
    pt_ip = re.compile(r"^ (?: %s | %s ) $" % (pt_ipv4, pt_ipv6), re.VERBOSE)

    def clean(self, value):
        if value is None:
            return None
        else:
            values = map(str.strip, self.separators.split(value))
            values = (value for value in values if len(value) != 0)
            values = map(self._clean_addr_or_host, values)
            return " ".join(values)

    def _clean_addr_or_host(self, value):
        looks_like_ip = self.pt_ip.match(value) is not None
        if looks_like_ip:
            return self._clean_addr(value)
        elif ":" in value:
            # This is probably an IPv6 address. It's definitely not a
            # hostname.
            return self._clean_addr(value)
        else:
            return self._clean_host(value)

    def _clean_addr(self, addr):
        try:
            addr = IPAddress(addr)
        except AddrFormatError as error:
            message = str(error)  # netaddr has good messages.
            message = message[:1].upper() + message[1:] + "."
            raise ValidationError(message)
        else:
            return str(addr)

    def _clean_host(self, host):
        try:
            validate_hostname(host)
        except ValidationError as error:
            raise ValidationError("Invalid hostname: " + error.message)
        else:
            return host


class SubnetListFormField(forms.CharField):
    """Accepts a space/comma separated list of hostnames, Subnets or IPs.

    This field normalizes the list to a space-separated list.
    """

    separators = re.compile(r"[,\s]+")

    # Regular expressions to sniff out things that look like IP addresses;
    # additional and more robust validation ought to be done to make sure.
    pt_ipv4 = r"(?: \d{1,3} [.] \d{1,3} [.] \d{1,3} [.] \d{1,3} )"
    pt_ipv6 = r"(?: (|[0-9A-Fa-f]{1,4}) [:] (|[0-9A-Fa-f]{1,4}) [:] (.*))"
    pt_ip = re.compile(r"^ (?: %s | %s ) $" % (pt_ipv4, pt_ipv6), re.VERBOSE)
    pt_subnet = re.compile(
        r"^ (?: %s | %s ) \/\d+$" % (pt_ipv4, pt_ipv6), re.VERBOSE
    )

    def clean(self, value):
        if value is None:
            return None
        else:
            values = map(str.strip, self.separators.split(value))
            values = (value for value in values if len(value) != 0)
            values = map(self._clean_addr_or_host, values)
            return " ".join(values)

    def _clean_addr_or_host(self, value):
        looks_like_ip = self.pt_ip.match(value) is not None
        looks_like_subnet = self.pt_subnet.match(value) is not None
        if looks_like_subnet:
            return self._clean_subnet(value)
        elif looks_like_ip:
            return self._clean_addr(value)
        else:
            return self._clean_host(value)

    def _clean_addr(self, value):
        try:
            addr = IPAddress(value)
        except ValueError:
            return
        except AddrFormatError:
            raise ValidationError("Invalid IP address: %s." % value)
        else:
            return str(addr)

    def _clean_subnet(self, value):
        try:
            cidr = IPNetwork(value)
        except AddrFormatError:
            raise ValidationError("Invalid network: %s." % value)
        else:
            return str(cidr)

    def _clean_host(self, host):
        try:
            validate_hostname(host)
        except ValidationError as error:
            raise ValidationError("Invalid hostname: " + error.message)
        else:
            return host


class CaseInsensitiveChoiceField(forms.ChoiceField):
    """ChoiceField that allows the input to be case insensitive."""

    def to_python(self, value):
        if value not in self.empty_values:
            value = value.lower()
        return super(CaseInsensitiveChoiceField, self).to_python(value)


class SpecifierOrModelChoiceField(forms.ModelChoiceField):
    """ModelChoiceField which is also able to accept input in the format
    of a specifiers string.
    """

    def to_python(self, value):
        try:
            return super(SpecifierOrModelChoiceField, self).to_python(value)
        except ValidationError as e:
            if isinstance(value, str):
                object_id = self.queryset.get_object_id(value)
                if object_id is None:
                    obj = get_one(
                        self.queryset.filter_by_specifiers(value),
                        exception_class=ValidationError,
                    )
                    if obj is not None:
                        return obj
                else:
                    try:
                        return self.queryset.get(id=object_id)
                    except ObjectDoesNotExist:
                        # Re-raising this as a ValidationError prevents the API
                        # from returning an internal server error rather than
                        # a bad request.
                        raise ValidationError("None found with id=%s." % value)
            raise e


class DomainNameField(CharField):
    """Custom Django field that strips whitespace and trailing '.' characters
    from DNS domain names before validating and saving to the database. Also,
    validates that the domain name is valid according to RFCs 952 and 1123.
    (Note that this field type should NOT be used for hostnames, since the set
    of valid hostnames is smaller than the set of valid domain names.)
    """

    def __init__(self, *args, **kwargs):
        validators = kwargs.pop("validators", [])
        validators.append(validate_domain_name)
        kwargs["validators"] = validators
        super(DomainNameField, self).__init__(*args, **kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super(DomainNameField, self).deconstruct()
        del kwargs["validators"]
        return name, path, args, kwargs

    # Here we are using (abusing?) the to_pytion() function to coerce and
    # normalize this type. Django does not have a function intended purely
    # to normalize before saving to the database, so to_python() is the next
    # closest alternative. For more information, see:
    # https://docs.djangoproject.com/en/1.6/ref/forms/validation/
    # https://code.djangoproject.com/ticket/6362
    def to_python(self, value):
        value = super(DomainNameField, self).to_python(value)
        if value is None:
            return None
        value = value.strip().rstrip(".")
        return value


class NodeChoiceField(forms.ModelChoiceField):
    def __init__(self, queryset, *args, **kwargs):
        super().__init__(queryset=queryset.distinct(), *args, **kwargs)

    def clean(self, value):
        if not value:
            return None
        # Avoid circular imports
        from maasserver.models.node import Node

        try:
            return self.queryset.get(Q(system_id=value) | Q(hostname=value))
        except Node.DoesNotExist:
            raise ValidationError(
                "Select a valid choice. "
                "%s is not one of the available choices." % value
            )

    def to_python(self, value):
        # Avoid circular imports
        from maasserver.models.node import Node

        try:
            return self.queryset.get(Q(system_id=value) | Q(hostname=value))
        except Node.DoesNotExist:
            raise ValidationError(
                "Select a valid choice. "
                "%s is not one of the available choices." % value
            )


class VersionedTextFileField(forms.ModelChoiceField):
    def __init__(self, *args, **kwargs):
        super().__init__(queryset=None, *args, **kwargs)

    def clean(self, value):
        if self.initial is None:
            if value is None:
                raise ValidationError("Must be given a value")
            # Create a new VersionedTextFile if one doesn't exist
            if isinstance(value, dict):
                return VersionedTextFile.objects.create(**value)
            else:
                return VersionedTextFile.objects.create(data=value)
        elif value is None:
            return self.initial
        else:
            # Create and return a new VersionedTextFile linked to the previous
            # VersionedTextFile
            if isinstance(value, dict):
                return self.initial.update(**value)
            else:
                return self.initial.update(value)


@deconstructible
class URLOrPPAValidator(URLValidator):
    message = _("Enter a valid repository URL or PPA location.")

    ppa_re = (
        r"ppa:" + URLValidator.hostname_re + r"/" + URLValidator.hostname_re
    )

    def __call__(self, value):
        match = re.search(URLOrPPAValidator.ppa_re, force_text(value))
        # If we don't have a PPA location, let URLValidator do its job.
        if not match:
            super().__call__(value)


class URLOrPPAFormField(forms.URLField):
    widget = forms.URLInput
    default_error_messages = {
        "invalid": _("Enter a valid repository URL or PPA location.")
    }
    default_validators = [URLOrPPAValidator()]

    def to_python(self, value):
        # Call grandparent method (CharField) to get string value.
        value = super(forms.URLField, self).to_python(value)
        # If it's a PPA locator, return it, else run URL pythonator.
        match = re.search(URLOrPPAValidator.ppa_re, value)
        return value if match else super().to_python(value)


class URLOrPPAField(URLField):
    default_validators = [URLOrPPAValidator()]
    description = _("URLOrPPAField")

    # Copied from URLField, with modified form_class.
    def formfield(self, **kwargs):
        defaults = {"form_class": URLOrPPAFormField}
        defaults.update(kwargs)
        return super(URLField, self).formfield(**defaults)
