# Copyright 2015-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""API handlers: `Domain`."""

from maasserver.api.support import (
    admin_method,
    OperationsHandler,
)
from maasserver.enum import NODE_PERMISSION
from maasserver.exceptions import MAASAPIValidationError
from maasserver.forms_domain import DomainForm
from maasserver.models import Domain
from piston3.utils import rc


DISPLAYED_DOMAIN_FIELDS = (
    'id',
    'name',
    'ttl',
    'authoritative',
    'resource_record_count',
)


class DomainsHandler(OperationsHandler):
    """Manage domains."""
    api_doc_section_name = "Domains"
    update = delete = None
    fields = DISPLAYED_DOMAIN_FIELDS

    @classmethod
    def resource_uri(cls, *args, **kwargs):
        # See the comment in NodeHandler.resource_uri.
        return ('domains_handler', [])

    def read(self, request):
        """List all domains."""
        return Domain.objects.all()

    @admin_method
    def create(self, request):
        """Create a domain.

        :param name: Name of the domain.
        :param authoritative: Class type of the domain.
        """
        form = DomainForm(data=request.data)
        if form.is_valid():
            return form.save()
        else:
            raise MAASAPIValidationError(form.errors)


class DomainHandler(OperationsHandler):
    """Manage domain."""
    api_doc_section_name = "Domain"
    create = None
    model = Domain
    fields = DISPLAYED_DOMAIN_FIELDS

    @classmethod
    def resource_uri(cls, domain=None):
        # See the comment in NodeHandler.resource_uri.
        domain_id = "domain_id"
        if domain is not None:
            domain_id = domain.id
        return ('domain_handler', (domain_id,))

    @classmethod
    def name(cls, domain):
        """Return the name of the domain."""
        return domain.get_name()

    @classmethod
    def resources(cls, domain):
        """Return DNSResources within the specified domain."""
        return domain.dnsresource_set.all()

    def read(self, request, domain_id):
        """Read domain.

        Returns 404 if the domain is not found.
        """
        return Domain.objects.get_domain_or_404(
            domain_id, request.user, NODE_PERMISSION.VIEW)

    def update(self, request, domain_id):
        """Update domain.

        :param name: Name of the domain.
        :param authoritative: True if we are authoritative for this domain.
        :param ttl: The default TTL for this domain.

        Returns 403 if the user does not have permission to update the
        dnsresource.
        Returns 404 if the domain is not found.
        """
        domain = Domain.objects.get_domain_or_404(
            domain_id, request.user, NODE_PERMISSION.ADMIN)
        form = DomainForm(instance=domain, data=request.data)
        if form.is_valid():
            return form.save()
        else:
            raise MAASAPIValidationError(form.errors)

    def delete(self, request, domain_id):
        """Delete domain.

        Returns 403 if the user does not have permission to update the
        dnsresource.
        Returns 404 if the domain is not found.
        """
        domain = Domain.objects.get_domain_or_404(
            domain_id, request.user, NODE_PERMISSION.ADMIN)
        domain.delete()
        return rc.DELETED
