# Copyright 2012-2015 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""MAAS-specific helpers for :class:`User`."""

__all__ = [
    "create_auth_token",
    "create_user",
    "get_auth_tokens",
    "get_creds_tuple",
    "SYSTEM_USERS",
]

from maasserver import worker_user
from maasserver.models import Config
from metadataserver import nodeinituser
from piston3.models import Consumer, Token

# Special users internal to MAAS.
SYSTEM_USERS = [
    # For nodes' access to the metadata API:
    nodeinituser.user_name,
    # For node-group's workers to the MAAS API:
    worker_user.user_name,
]

GENERIC_CONSUMER = "MAAS consumer"


def create_auth_token(user, consumer_name=None):
    """Create new Token and Consumer (OAuth authorisation) for `user`.

    :param user: The user to create a token for.
    :type user: User
    :param consumer_name: Name of the consumer to be assigned to the newly
     generated token.
    :return: The created Token.
    :rtype: piston.models.Token

    """
    if consumer_name is None:
        consumer_name = GENERIC_CONSUMER
    consumer = Consumer.objects.create(
        user=user, name=consumer_name, status="accepted"
    )
    consumer.generate_random_codes()
    # This is a 'generic' consumer aimed to service many clients, hence
    # we don't authenticate the consumer with key/secret key.
    consumer.secret = ""
    consumer.save()
    token = Token.objects.create(
        user=user, token_type=Token.ACCESS, consumer=consumer, is_approved=True
    )
    token.generate_random_codes()
    return token


def get_auth_tokens(user):
    """Fetches all the user's OAuth tokens.

    :return: A QuerySet of the tokens.
    :rtype: django.db.models.query.QuerySet_

    .. _django.db.models.query.QuerySet: https://docs.djangoproject.com/
       en/dev/ref/models/querysets/

    """
    return (
        Token.objects.select_related()
        .filter(user=user, token_type=Token.ACCESS, is_approved=True)
        .order_by("id")
    )


# When a user is created: create the related profile, and the default
# consumer/token. Also add the user to the default group.
def create_user(sender, instance, created, **kwargs):
    # Avoid circular imports.
    from maasserver.models.userprofile import UserProfile

    # System users do not have profiles.
    if created and instance.username not in SYSTEM_USERS:
        is_local = not Config.objects.is_external_auth_enabled()
        # Create related UserProfile.
        profile = UserProfile.objects.create(user=instance, is_local=is_local)

        # Create initial authorisation token.
        profile.create_authorisation_token()


def get_creds_tuple(token):
    """Return API credentials as tuple, as used in :class:`MAASOAuth`.

    Returns a tuple of (consumer key, resource token, resource secret).
    The consumer secret is hard-wired to the empty string.
    """
    return (token.consumer.key, token.key, token.secret)
