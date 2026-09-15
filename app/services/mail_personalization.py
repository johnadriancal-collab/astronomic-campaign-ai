"""
Astronomic Mail send-time personalization -- P0 fix (2026-09-15).

Confirmed production bug this closes: {{first_name}}/{{last_name}}/
{{company}} were validated at campaign-authoring time
(find_unknown_mail_template_variables(), app/models/mail.py) -- an
unknown token is rejected when a step is written -- but nothing in the
send path ever substituted a real value for an ALLOWED token. Every real
recipient would have received the literal text "{{first_name}}".

Deliberately NOT a template engine: exact-token substitution only (a
regex matching the literal "{{name}}" shape, restricted to
ALLOWED_MAIL_TEMPLATE_VARIABLES) -- no expression evaluation, no
conditionals/loops, nothing beyond "swap this one known token for this
one known value." This is a scope limit chosen on purpose: running
arbitrary template logic against real contact data is a real
injection-shaped risk, and this module is deliberately incapable of it.

Pure -- no I/O, no store access, never mutates the authored template.
MailSequenceStep.subject/body and MailEnrollmentStep.subject/body stay
byte-identical forever; only the transient RENDERED string handed to
compose_outbound_email()/MailSendRequest differs per recipient.

Fails LOUD, never silent: render_mail_template() raises
MailPersonalizationError for any allowed variable present in the
template whose value can't be resolved to a non-blank string. It never
returns a string containing an unresolved "{{...}}", and never silently
drops a token by rendering it as empty text.

MailPersonalizationError is a ValueError on purpose: MailSendingService.
prepare_and_send_step() already classifies any ValueError raised while
composing a message as branch D of _handle_prepare_failure() --
"Permanently invalid preparation... retrying an identical request
against an identical recipient would fail identically" -- which fails
the step and the enrollment (no further steps sent to that recipient)
WITHOUT ever reaching sender.prepare()/send_prepared(), i.e. strictly
before any provider call. No new failure-handling branch was added for
this; the existing, already-tested taxonomy already does exactly the
right thing for "a required personalization value is missing."
"""

import re

from app.models.crm import CrmContact
from app.models.mail import ALLOWED_MAIL_TEMPLATE_VARIABLES

_TOKEN_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


class MailPersonalizationError(ValueError):
    """Raised by render_mail_template() when a template references an
    allowed variable whose resolved value is missing/blank, or --
    defensively, since authoring-time validation should already have
    caught this -- an unknown variable. See this module's own docstring
    for why being a ValueError is load-bearing, not incidental."""


def contact_personalization_variables(contact: CrmContact | None) -> dict[str, str | None]:
    """The ONE place ALLOWED_MAIL_TEMPLATE_VARIABLES's abstract names are
    mapped onto this app's real CrmContact fields, so that mapping is
    never duplicated or allowed to drift between call sites. `contact`
    may be None (e.g. the enrolled Contact was deleted after enrollment)
    -- returns an all-missing dict rather than raising itself; whether
    that's actually a problem depends entirely on whether the template
    being rendered needs any of these values at all, which is
    render_mail_template()'s job to decide, not this function's."""
    if contact is None:
        return {}
    return {
        "first_name": contact.first_name,
        "last_name": contact.last_name,
        "company": contact.company,
    }


def render_mail_template(template: str, variables: dict[str, str | None]) -> str:
    """Substitutes every "{{name}}" token in `template`. `variables` is
    expected to already be scoped to ALLOWED_MAIL_TEMPLATE_VARIABLES keys
    (callers build it via contact_personalization_variables()) -- this
    function re-validates that independently anyway, since it is the one
    place that actually decides what reaches Gmail; defense in depth,
    never trust-the-caller-alone for something this consequential.

    A template with no "{{...}}" tokens at all is returned completely
    unchanged, including when `variables` is empty -- personalization is
    optional per step, never required, and the common case (a step with
    no tokens) must never depend on contact data resolving at all.

    Raises MailPersonalizationError, never returns a partially-rendered
    or token-containing string, for:
      - a token naming something outside ALLOWED_MAIL_TEMPLATE_VARIABLES
        (authoring-time validation should already prevent this; this is
        a second, independent gate, not a trust boundary this function
        assumes was already enforced upstream)
      - a token naming an allowed variable whose value is missing, None,
        or blank/whitespace-only
    """

    def _replace(match: re.Match) -> str:
        name = match.group(1)
        if name not in ALLOWED_MAIL_TEMPLATE_VARIABLES:
            raise MailPersonalizationError(
                f"Template references '{{{{{name}}}}}', which is not an allowed personalization variable."
            )
        value = variables.get(name)
        if not value or not value.strip():
            raise MailPersonalizationError(
                f"Cannot resolve required personalization variable '{{{{{name}}}}}' -- no value on file for this recipient."
            )
        return value

    return _TOKEN_PATTERN.sub(_replace, template)
