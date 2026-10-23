from aiocache import cached

from app.db import GetDB
from app.db.crud.client_profile import get_all_client_profiles
from app.db.crud.client_template import TEMPLATE_TYPE_TO_LEGACY_KEY
from app.models.client_profile import ClientProfile, ClientProfileTemplateType
from app.models.client_template import ClientTemplateType
from app.models.settings import RULE_TEMPLATE_TYPES, ConfigFormat, Subscription as SubSettings
from app.subscription.client_templates import subscription_client_templates, subscription_template_contents
from app.subscription.config_cache import clear_sub_config_cache
from app.utils.logger import get_logger

logger = get_logger("subscription")

# Settings a profile overrides directly over the global subscription settings (first non-empty wins).
_GLOBAL_OVERRIDE_FIELDS = ("announce", "announce_url", "update_interval", "randomize_order")


@cached()
async def subscription_client_profiles() -> dict[int, ClientProfile]:
    async with GetDB() as db:
        return {profile.id: ClientProfile.model_validate(profile) for profile in await get_all_client_profiles(db)}


async def get_client_profile(profile_id: int | None) -> ClientProfile | None:
    if profile_id is None:
        return None
    return (await subscription_client_profiles()).get(profile_id)


async def refresh_client_profiles_cache() -> None:
    await subscription_client_profiles.cache.clear()
    clear_sub_config_cache()


async def handle_client_profile_message(_: dict) -> None:
    """Handle client profile update messages from NATS router."""
    await refresh_client_profiles_cache()


def rule_template_type(config_format: str) -> ClientTemplateType | None:
    """Client template type a subscription rule's template_id points to for an output format."""
    try:
        return RULE_TEMPLATE_TYPES.get(ConfigFormat(config_format))
    except ValueError:
        return None


def resolve_client_template(
    template_type: ClientTemplateType,
    profile: ClientProfile | None,
    rule_template_id: int | None,
    defaults: dict[str, str],
    contents: dict[ClientTemplateType, dict[int, str]],
) -> str | None:
    """Template content for one whole-config type: subscription rule > client profile > global default.

    Host-level xray templates are applied per host on top of this (XrayConfiguration.add), so they beat all three.
    """
    contents_by_id = contents.get(template_type, {})
    if rule_template_id is not None:
        if content := contents_by_id.get(rule_template_id):
            return content
        # Settings validation and template deletion keep this from happening; fall back to the next source.
        logger.warning(f"Subscription rule template {rule_template_id} ({template_type.value}) not found")
    if profile is not None:
        template_id = profile.templates.get(ClientProfileTemplateType(template_type.value))
        if template_id is not None and (content := contents_by_id.get(template_id)):
            return content
    return defaults.get(TEMPLATE_TYPE_TO_LEGACY_KEY[template_type])


async def resolve_client_templates(
    profile: ClientProfile | None,
    config_format: str | None = None,
    rule_template_id: int | None = None,
) -> dict[str, str]:
    """Legacy-keyed client templates (as subscription_client_templates) with the rule and profile choices applied.

    The rule template only applies to the type the requested format renders; profile templates apply to every type.
    """
    defaults = await subscription_client_templates()
    rule_type = rule_template_type(config_format) if rule_template_id is not None and config_format else None
    template_types = (
        {ClientTemplateType(template_type.value) for template_type in profile.templates} if profile else set()
    )
    if rule_type is not None:
        template_types.add(rule_type)
    if not template_types:
        return defaults

    contents = await subscription_template_contents()
    resolved = dict(defaults)
    for template_type in template_types:
        content = resolve_client_template(
            template_type,
            profile,
            rule_template_id if template_type == rule_type else None,
            defaults,
            contents,
        )
        if content is not None:
            resolved[TEMPLATE_TYPE_TO_LEGACY_KEY[template_type]] = content
    return resolved


def apply_client_profile_settings(sub_settings: SubSettings, profile: ClientProfile | None) -> SubSettings:
    """Global subscription settings with the profile's overrides.

    profile_title, support_url (profile > admin > global), custom variables (global, admin, profile)
    and remove_response_headers are applied where they are used, since they also involve the admin
    or the final header set.
    """
    if profile is None:
        return sub_settings

    overrides = profile.settings
    update = {}
    for field in _GLOBAL_OVERRIDE_FIELDS:
        value = getattr(overrides, field)
        if value is not None:
            update[field] = value

    if overrides.response_headers:
        overridden = {name.lower() for name in overrides.response_headers}
        response_headers = {
            name: value for name, value in sub_settings.response_headers.items() if str(name).lower() not in overridden
        }
        response_headers.update(overrides.response_headers)
        update["response_headers"] = response_headers

    return sub_settings.model_copy(update=update) if update else sub_settings


def drop_removed_response_headers(headers: dict[str, str], profile: ClientProfile | None) -> dict[str, str]:
    """Remove the profile's remove_response_headers from a header dict (case-insensitive)."""
    if profile is None or not profile.settings.remove_response_headers:
        return headers
    removed = {name.lower() for name in profile.settings.remove_response_headers}
    return {name: value for name, value in headers.items() if name.lower() not in removed}
