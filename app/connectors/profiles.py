"""Four named deployments; no platform accounts are created by writing a profile."""

import json
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import ValidationError

from app.connectors.config import ConnectorSettings


@dataclass(frozen=True)
class Profile:
    provider: str
    bot: str
    port: int

    @property
    def name(self):
        return f"{self.provider}-{self.bot}"

    @property
    def project(self):
        return self.name.replace("_", "-")

    @property
    def stage(self):
        return "mvp" if self.bot == "nevidima_mvp_bot" else "production"

    @property
    def inbox_name(self):
        return f"{self.bot} / {self.provider} / {self.stage}"


PROFILES = (
    Profile("vk", "nevidima_bot", 8091),
    Profile("vk", "nevidima_mvp_bot", 8092),
    Profile("whatsapp", "nevidima_bot", 8093),
    Profile("whatsapp", "nevidima_mvp_bot", 8094),
)


class FileSettings(ConnectorSettings):
    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings,
                                  dotenv_settings, file_secret_settings):
        # Missing values in MVP must never be filled from a production process's
        # environment or from the repository .env.
        return (init_settings,)


def read_values(directory: Path, profile: Profile):
    path = directory / f"{profile.name}.env"
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("profile file must be private and not a symlink")
    return {k: v or "" for k, v in dotenv_values(path, interpolate=False).items()}


def settings_from_values(values):
    return FileSettings(**{k.removeprefix("CONNECTOR_").lower(): v for k, v in values.items()
                           if k.startswith("CONNECTOR_")})


def initialize(directory: Path, *, webhook_domain="", chatwoot_url="", image_version="pending"):
    if webhook_domain and not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", webhook_domain):
        raise ValueError("invalid webhook domain")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", image_version):
        raise ValueError("invalid image version")
    directory = directory.absolute()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_mode & 0o077:
        raise ValueError("profile directory must have mode 0700")
    paths = [directory / f"{p.name}.env" for p in PROFILES]
    if any(path.exists() or path.is_symlink() for path in paths):
        raise FileExistsError("profile files already exist; refusing to replace credentials")
    created = []
    try:
        for profile, path in zip(PROFILES, paths, strict=True):
            values = {
                "CONNECTOR_PROFILE": profile.bot,
                "CONNECTOR_STAGE": profile.stage,
                "CONNECTOR_PROJECT": profile.project,
                "CONNECTOR_ENV_FILE": str(path),
                "CONNECTOR_IMAGE": f"localhost/{profile.project}:{image_version}",
                "CONNECTOR_BIND_PORT": str(profile.port),
                "CONNECTOR_PROVIDER": profile.provider,
                "CONNECTOR_PUBLIC_URL": (f"https://{profile.project}.{webhook_domain}"
                                         if webhook_domain else ""),
                "CONNECTOR_CHATWOOT_INBOX_NAME": profile.inbox_name,
                "CONNECTOR_CHATWOOT_URL": chatwoot_url,
                "CONNECTOR_ACCOUNT_ID": "",
                "CONNECTOR_INBOX_ID": "",
                "CONNECTOR_API_TOKEN": "",
                "CONNECTOR_INBOX_IDENTIFIER": "",
                "CONNECTOR_INBOX_HMAC_TOKEN": "",
                "CONNECTOR_CALLBACK_SECRET": secrets.token_urlsafe(32),
                "CONNECTOR_ATTACHMENT_HOSTS": "",
            }
            if profile.provider == "vk":
                values.update({
                    "CONNECTOR_VK_GROUP_ID": "", "CONNECTOR_VK_TOKEN": "",
                    "CONNECTOR_VK_SECRET": secrets.token_urlsafe(32),
                    "CONNECTOR_VK_CONFIRMATION": "", "CONNECTOR_VK_API_VERSION": "5.199",
                })
            else:
                values.update({
                    "CONNECTOR_WHATSAPP_PHONE_ID": "", "CONNECTOR_WHATSAPP_BUSINESS_ID": "",
                    "CONNECTOR_WHATSAPP_TOKEN": "", "CONNECTOR_WHATSAPP_APP_SECRET": "",
                    "CONNECTOR_WHATSAPP_VERIFY_TOKEN": secrets.token_urlsafe(32),
                    "CONNECTOR_WHATSAPP_API_VERSION": "",
                })
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created.append(path)
            with os.fdopen(descriptor, "w") as output:
                output.write("# Private connector profile; fill provider and Chatwoot credentials.\n")
                for key, value in values.items():
                    output.write(f"{key}={json.dumps(value, ensure_ascii=False)}\n")
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    return paths


def inspect_profiles(directory: Path):
    reports, loaded = [], []
    for profile in PROFILES:
        report = {"profile": profile.name, "project": profile.project,
                  "inbox_name": profile.inbox_name, "ready": False, "missing": [], "errors": []}
        reports.append(report)
        try:
            values = read_values(directory, profile)
        except (OSError, ValueError):
            report["errors"].append("private_profile_file_unavailable")
            continue
        loaded.append((profile, values, report))
        required = ["CHATWOOT_URL", "ACCOUNT_ID", "INBOX_ID", "API_TOKEN", "INBOX_IDENTIFIER",
                    "INBOX_HMAC_TOKEN", "PUBLIC_URL", "CALLBACK_SECRET", "ENV_FILE",
                    "IMAGE", "BIND_PORT"]
        required += (["VK_GROUP_ID", "VK_TOKEN", "VK_SECRET", "VK_CONFIRMATION"]
                     if profile.provider == "vk" else [
                         "WHATSAPP_PHONE_ID", "WHATSAPP_BUSINESS_ID", "WHATSAPP_TOKEN",
                         "WHATSAPP_APP_SECRET", "WHATSAPP_VERIFY_TOKEN", "WHATSAPP_API_VERSION"])
        report["missing"] = ["CONNECTOR_" + key for key in required
                             if values.get("CONNECTOR_" + key, "").strip() in {"", "0"}]
        if (values.get("CONNECTOR_PROFILE") != profile.bot
                or values.get("CONNECTOR_PROVIDER") != profile.provider
                or values.get("CONNECTOR_PROJECT") != profile.project
                or values.get("CONNECTOR_STAGE") != profile.stage):
            report["errors"].append("profile_identity_mismatch")
        try:
            public = urlsplit(values.get("CONNECTOR_PUBLIC_URL", ""))
            valid_public = (public.scheme == "https" and bool(public.hostname)
                            and public.username is None and public.password is None
                            and not (public.path or public.query or public.fragment)
                            and public.port in {None, 443})
        except ValueError:
            valid_public = False
        if not valid_public:
            report["errors"].append("public_https_origin_required")
        port = values.get("CONNECTOR_BIND_PORT", "")
        if not port.isascii() or not port.isdigit() or not 1 <= int(port) <= 65535:
            report["errors"].append("invalid_bind_port")
        if not report["missing"]:
            try:
                settings_from_values(values)
            except (ValidationError, ValueError):
                report["errors"].append("invalid_connector_configuration")

    seen = {}

    def numeric(value):
        return str(int(value)) if value and value.isascii() and value.isdigit() else value

    def origin(value):
        return value.rstrip("/").lower() if value else value

    for profile, values, report in loaded:
        identity = ("CONNECTOR_VK_GROUP_ID" if profile.provider == "vk"
                    else "CONNECTOR_WHATSAPP_PHONE_ID")
        bindings = {
            "provider_identity": (profile.provider, numeric(values.get(identity))),
            "chatwoot_inbox": (origin(values.get("CONNECTOR_CHATWOOT_URL")),
                               numeric(values.get("CONNECTOR_ACCOUNT_ID")),
                               numeric(values.get("CONNECTOR_INBOX_ID"))),
            **{key: (values.get(key),) for key in (
                "CONNECTOR_API_TOKEN", "CONNECTOR_CALLBACK_SECRET", "CONNECTOR_INBOX_IDENTIFIER",
                "CONNECTOR_INBOX_HMAC_TOKEN", "CONNECTOR_PUBLIC_URL", "CONNECTOR_BIND_PORT")},
        }
        bindings["CONNECTOR_BIND_PORT"] = (numeric(values.get("CONNECTOR_BIND_PORT")),)
        bindings["CONNECTOR_PUBLIC_URL"] = (origin(values.get("CONNECTOR_PUBLIC_URL")),)
        for kind, binding in bindings.items():
            if any(part in {None, "", "0"} for part in binding):
                continue
            key = (kind, *binding)
            if key in seen:
                report["errors"].append("shared_" + kind.lower())
                seen[key]["errors"].append("shared_" + kind.lower())
            else:
                seen[key] = report
    for report in reports:
        report["errors"] = sorted(set(report["errors"]))
        report["ready"] = not (report["missing"] or report["errors"])
    return reports
