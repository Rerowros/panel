"""Client profiles, per-rule templates, template directives and plugin hooks working together."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import status

from app.plugins import registry as plugin_registry
from app.settings import refresh_caches
from app.subscription.config_cache import make_sub_config_key
from tests.api import client
from tests.api.helpers import (
    auth_headers,
    create_client_template,
    create_core,
    create_group,
    create_user,
    delete_client_template,
    delete_core,
    delete_group,
    delete_user,
    unique_name,
)

RULE_UA = "ParityRuleClient"
OTHER_UA = "ParityOtherClient"


def _xray_template_content(marker: str, **directive) -> str:
    content = {
        "inbounds": [{"tag": "socks", "protocol": "socks", "port": 10808}],
        "outbounds": [{"tag": marker, "protocol": "freedom", "settings": {}}],
    }
    if directive:
        content["pasarguard"] = directive
    return json.dumps(content)


class Env:
    def __init__(self, access_token: str):
        self.token = access_token
        self.headers = auth_headers(access_token)
        self.inbound = unique_name("parity_inbound")
        self.core = create_core(
            access_token,
            config={
                "log": {"loglevel": "info"},
                "inbounds": [
                    {
                        "tag": self.inbound,
                        "listen": "0.0.0.0",
                        "port": 2088,
                        "protocol": "vmess",
                        "settings": {"clients": []},
                        "streamSettings": {"network": "ws", "wsSettings": {"path": "/p"}, "security": "none"},
                    }
                ],
                "outbounds": [{"protocol": "freedom", "tag": "DIRECT"}],
            },
            fallbacks=[],
        )
        self.group = create_group(access_token, inbound_tags=[self.inbound])
        self.host_ids: list[int] = []
        self.template_ids: list[int] = []
        self.profile_ids: list[int] = []
        self.usernames: list[str] = []

    def add_host(self, remark: str, **extra) -> dict:
        response = client.post(
            "/api/host",
            headers=self.headers,
            json={
                "remark": remark,
                "address": ["198.51.100.80"],
                "port": 443,
                "sni": ["parity.example.com"],
                "inbound_tag": self.inbound,
                "priority": len(self.host_ids) + 1,
                **extra,
            },
        )
        assert response.status_code == status.HTTP_201_CREATED, response.text
        self.host_ids.append(response.json()["id"])
        return response.json()

    def xray_template(self, marker: str, **directive) -> dict:
        template = create_client_template(
            self.token,
            name=unique_name(marker),
            template_type="xray_subscription",
            content=_xray_template_content(marker, **directive),
        )
        self.template_ids.append(template["id"])
        return template

    def profile(self, **payload) -> dict:
        payload.setdefault("name", unique_name("parity_profile"))
        response = client.post("/api/client_profile", headers=self.headers, json=payload)
        assert response.status_code == status.HTTP_201_CREATED, response.text
        self.profile_ids.append(response.json()["id"])
        return response.json()

    def user(self, profile: dict | None = None) -> dict:
        payload = {"client_profile_id": profile["id"]} if profile else {}
        user = create_user(self.token, group_ids=[self.group["id"]], payload=payload)
        self.usernames.append(user["username"])
        return user

    def cleanup(self) -> None:
        for username in self.usernames:
            delete_user(self.token, username)
        for host_id in self.host_ids:
            client.delete(f"/api/host/{host_id}", headers=self.headers)
        for profile_id in self.profile_ids:
            client.delete(f"/api/client_profile/{profile_id}", headers=self.headers)
        for template_id in self.template_ids:
            delete_client_template(self.token, template_id)
        delete_group(self.token, self.group["id"])
        delete_core(self.token, self.core["id"])


@pytest.fixture
def env(access_token):
    environment = Env(access_token)
    yield environment
    environment.cleanup()


@pytest.fixture
def parity_rules(mock_settings):
    """Prepend rules to the subscription settings snapshot tests/api serve subscriptions from."""
    subscription = mock_settings["subscription"]
    original_rules, original_title = subscription["rules"], subscription["profile_title"]

    def apply(rule_template_id: int, profile_title: str | None = None) -> None:
        subscription["rules"] = [
            {"pattern": f"^{RULE_UA}$", "target": "xray", "template_id": rule_template_id},
            {"pattern": f"^{OTHER_UA}$", "target": "xray"},
            *original_rules,
        ]
        if profile_title is not None:
            subscription["profile_title"] = profile_title
        # Subscription settings are cached; drop what earlier tests loaded
        asyncio.run(refresh_caches())

    yield apply
    subscription["rules"], subscription["profile_title"] = original_rules, original_title
    asyncio.run(refresh_caches())


def _get(user: dict, user_agent: str):
    response = client.get(user["subscription_url"], headers={"User-Agent": user_agent})
    assert response.status_code == status.HTTP_200_OK, response.text
    return response.json()


def _last_tags(configs: list[dict]) -> list[str]:
    return [config["outbounds"][-1]["tag"] for config in configs]


def test_rule_template_beats_profile_single_mode_template(env, parity_rules):
    env.add_host(remark="parity-a")
    env.add_host(remark="parity-b")
    rule_template = env.xray_template("parity-rule-marker")
    profile_template = env.xray_template("parity-profile-marker", mode="single", inject=[{"tag_prefix": "pool"}])
    profile = env.profile(
        templates={"xray_subscription": profile_template["id"]},
        settings={"profile_title": "Profile {USERNAME}"},
    )
    parity_rules(rule_template["id"], profile_title="Global {USERNAME}")
    user = env.user(profile)
    plain = env.user()

    # Matching user agent: the rule template wins over the profile template, one config per host
    rule_configs = _get(user, RULE_UA)
    assert _last_tags(rule_configs) == ["parity-rule-marker", "parity-rule-marker"]
    assert "parity-profile-marker" not in json.dumps(rule_configs)

    # Any other client: the profile's single-mode template, titled with the profile title
    (single,) = _get(user, OTHER_UA)
    assert "pasarguard" not in single
    assert single["remarks"] == f"Profile {user['username']}"
    tags = [outbound["tag"] for outbound in single["outbounds"]]
    assert tags[-1] == "parity-profile-marker"
    assert len([tag for tag in tags if tag.startswith("pool-")]) == 2

    # Back to the rule client right after: cached outputs of the two clients do not mix
    assert _last_tags(_get(user, RULE_UA)) == ["parity-rule-marker", "parity-rule-marker"]

    # A user without a profile gets the rule template for the rule client and the global default otherwise
    assert _last_tags(_get(plain, RULE_UA)) == ["parity-rule-marker", "parity-rule-marker"]
    plain_configs = _get(plain, OTHER_UA)
    assert len(plain_configs) == 2
    assert "parity-profile-marker" not in json.dumps(plain_configs)
    assert "parity-rule-marker" not in json.dumps(plain_configs)


def test_host_template_beats_rule_and_profile_templates(env, parity_rules):
    host_template = env.xray_template("parity-host-marker")
    rule_template = env.xray_template("parity-rule-marker")
    profile_template = env.xray_template("parity-profile-marker")
    env.add_host(remark="parity-own-template", subscription_templates={"xray": host_template["id"]})
    env.add_host(remark="parity-shared-template")
    parity_rules(rule_template["id"])
    user = env.user(env.profile(templates={"xray_subscription": profile_template["id"]}))

    by_remark = {config["remarks"]: config["outbounds"][-1]["tag"] for config in _get(user, RULE_UA)}
    assert by_remark == {"parity-own-template": "parity-host-marker", "parity-shared-template": "parity-rule-marker"}

    by_remark = {config["remarks"]: config["outbounds"][-1]["tag"] for config in _get(user, OTHER_UA)}
    assert by_remark == {
        "parity-own-template": "parity-host-marker",
        "parity-shared-template": "parity-profile-marker",
    }


def test_plugin_hooks_run_with_profile_variables(env, monkeypatch):
    def plugin_variables(_user: dict) -> dict:
        return {"BRAND": "plugin", "PLUGIN_ONLY": "from-plugin", "USERNAME": "hijacked"}

    def plugin_headers(_user: dict, _config_format: str | None, _headers: dict) -> dict:
        return {"X-Parity-Plugin": "on"}

    env.add_host(remark="{BRAND}-{PLUGIN_ONLY}-{USERNAME}")
    user = env.user(env.profile(settings={"custom_variables": [{"key": "BRAND", "value": "profile"}]}))
    fake_plugin = SimpleNamespace(
        subscription=SimpleNamespace(variable_hooks=[plugin_variables], header_hooks=[plugin_headers], transforms=[]),
        logger=SimpleNamespace(exception=lambda *_args, **_kwargs: None),
    )
    # Only for the subscription request; events of the setup calls above stay out of the fake plugin
    monkeypatch.setattr(plugin_registry, "active", [fake_plugin])

    response = client.get(f"{user['subscription_url']}/xray")
    assert response.status_code == status.HTTP_200_OK, response.text
    # Profile variable beats the plugin one, the plugin adds its own key, built-ins stay untouched
    assert [config["remarks"] for config in response.json()] == [f"profile-from-plugin-{user['username']}"]
    assert response.headers["x-parity-plugin"] == "on"


def test_sub_config_cache_key_separates_rule_template_and_profile():
    def user(profile_id: int | None) -> SimpleNamespace:
        return SimpleNamespace(
            id=7, status=None, inbounds=["tag"], data_limit=None, expire=None, client_profile_id=profile_id
        )

    keys = {
        make_sub_config_key(user(profile_id), "xray", False, False, template_id)
        for profile_id in (None, 1, 2)
        for template_id in (None, 10, 11)
    }
    assert len(keys) == 9
    assert make_sub_config_key(user(1), "xray", False, False, 10) == make_sub_config_key(
        user(1), "xray", False, False, 10
    )
