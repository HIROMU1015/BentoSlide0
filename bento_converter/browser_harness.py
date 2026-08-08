"""Shared deterministic Chromium lifecycle and environment diagnostics."""

from __future__ import annotations

import hashlib
import json
import os
import platform
from contextlib import contextmanager
from importlib import metadata
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

from .errors import BrowserCheckError


ENVIRONMENT_FORMAT = "bento/browser-environment/v1"
FONT_ENVIRONMENT_FORMAT = "bento/used-font-environment/v1"
NETWORK_POLICY_FORMAT = "bento/local-only/v1"

PROFILE_CONFIGS: dict[str, dict[str, Any]] = {
    "sourceLayout": {
        "viewport": {"width": 1400, "height": 900},
        "deviceScaleFactor": 1,
    },
    "bentoCheck": {
        "viewport": {"width": 1600, "height": 1000},
        "deviceScaleFactor": 1,
    },
}

_ANIMATION_GUARD_JS = r"""
(() => {
  const install = () => {
    if (!document.documentElement || document.getElementById('bento-browser-harness-style')) return;
    const style = document.createElement('style');
    style.id = 'bento-browser-harness-style';
    style.textContent = '*,*::before,*::after{animation:none!important;transition:none!important;caret-color:transparent!important;scroll-behavior:auto!important}';
    document.documentElement.appendChild(style);
  };
  install();
  if (!document.documentElement) new MutationObserver(install).observe(document, {childList:true,subtree:true});
})()
"""

_SETTLE_JS = r"""
async () => {
  if (document.fonts) await document.fonts.ready;
  await Promise.all([...document.images].map(async image => {
    if (!image.complete) {
      await new Promise(resolve => {
        image.addEventListener('load', resolve, {once:true});
        image.addEventListener('error', resolve, {once:true});
      });
    }
    if (typeof image.decode === 'function') {
      try { await image.decode(); } catch (_) {}
    }
  }));
  await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
}
"""

_FONT_OBSERVATION_JS = r"""
() => {
  const fontFaces = document.fonts ? [...document.fonts].map(face => ({
    family: String(face.family || ''),
    style: String(face.style || 'normal'),
    weight: String(face.weight || 'normal'),
    stretch: String(face.stretch || 'normal'),
    status: String(face.status || 'unknown')
  })) : [];
  const computedFamilies = [...new Set([...document.querySelectorAll('body *')]
    .filter(element => element.childNodes && [...element.childNodes].some(node => node.nodeType === Node.TEXT_NODE && node.textContent.trim()))
    .map(element => getComputedStyle(element).fontFamily)
    .filter(Boolean))].sort();
  return {
    locale: navigator.language || null,
    timezoneId: Intl.DateTimeFormat().resolvedOptions().timeZone || null,
    viewport: {width: innerWidth, height: innerHeight},
    deviceScaleFactor: devicePixelRatio,
    fontFaces,
    computedFamilies
  };
}
"""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def canonical_digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _playwright_version() -> str:
    try:
        return metadata.version("playwright")
    except metadata.PackageNotFoundError:
        return "unknown"


def _chrome_candidates(explicit: str | Path | None) -> list[Path]:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    configured = os.environ.get("BENTO_CHROME_PATH")
    if configured:
        candidates.append(Path(configured))
    candidates.extend(
        [
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
            Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        ]
    )
    return candidates


def find_browser_executable(explicit: str | Path | None = None) -> Path | None:
    for candidate in _chrome_candidates(explicit):
        if candidate.is_file():
            return candidate.resolve()
    return None


class BrowserHarness:
    """Own one Chromium process and isolated deterministic contexts per build phase."""

    def __init__(self, browser_executable: str | Path | None = None) -> None:
        self.executable = find_browser_executable(browser_executable)
        self.playwright: Any | None = None
        self.browser: Any | None = None
        self.browser_label = self.executable.name if self.executable else "Playwright Chromium"
        self._profiles: dict[str, dict[str, Any]] = {}
        self._blocked_requests: list[dict[str, str]] = []
        self._embedded_fonts: list[dict[str, str]] = []

    def __enter__(self) -> "BrowserHarness":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserCheckError(
                "Playwright is required for browser conversion. Install requirements-browser.txt."
            ) from exc
        try:
            self.playwright = sync_playwright().start()
            launch: dict[str, Any] = {
                "headless": True,
                "args": ["--allow-file-access-from-files", "--disable-background-networking"],
            }
            if self.executable:
                launch["executable_path"] = str(self.executable)
            self.browser = self.playwright.chromium.launch(**launch)
            return self
        except Exception as exc:
            self.close()
            raise BrowserCheckError(f"Cannot start deterministic Chromium: {exc}") from exc

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self.browser is not None:
            try:
                self.browser.close()
            finally:
                self.browser = None
        if self.playwright is not None:
            try:
                self.playwright.stop()
            finally:
                self.playwright = None

    @staticmethod
    def _request_allowed(url: str) -> bool:
        parsed = urlsplit(url)
        if parsed.scheme in {"file", "data", "blob", "about"}:
            return True
        if parsed.scheme in {"http", "https"}:
            return (parsed.hostname or "").lower() in {"127.0.0.1", "localhost", "::1"}
        return False

    def _route(self, route: Any, request: Any) -> None:
        if self._request_allowed(request.url):
            route.continue_()
            return
        parsed = urlsplit(request.url)
        self._blocked_requests.append({
            "scheme": parsed.scheme or "unknown",
            "host": parsed.hostname or "",
            "resourceType": request.resource_type,
        })
        route.abort("blockedbyclient")

    @contextmanager
    def page(self, profile: str) -> Iterator[Any]:
        if self.browser is None:
            raise BrowserCheckError("BrowserHarness is not running")
        if profile not in PROFILE_CONFIGS:
            raise BrowserCheckError(f"Unknown browser profile: {profile}")
        config = PROFILE_CONFIGS[profile]
        context = self.browser.new_context(
            viewport=config["viewport"],
            device_scale_factor=config["deviceScaleFactor"],
            reduced_motion="reduce",
            color_scheme="light",
            locale="en-US",
            timezone_id="UTC",
        )
        context.route("**/*", self._route)
        context.add_init_script(_ANIMATION_GUARD_JS)
        page = context.new_page()
        try:
            yield page
        finally:
            context.close()

    def settle(self, page: Any) -> None:
        page.evaluate(_SETTLE_JS)

    def assert_no_blocked_network(self) -> None:
        if not self._blocked_requests:
            return
        requests = sorted({(item["scheme"], item["host"], item["resourceType"]) for item in self._blocked_requests})
        summary = ", ".join(f"{scheme}://{host} ({kind})" for scheme, host, kind in requests)
        raise BrowserCheckError(
            "Deterministic conversion blocked remote or unsupported browser requests: " + summary
        )

    def _platform_fonts(self, page: Any) -> tuple[list[dict[str, Any]], bool]:
        """Collect fonts actually used for direct text nodes without exposing host paths."""

        session = None
        probe_attribute = "data-bento-font-probe"
        try:
            page.evaluate(
                """attribute => {
                  let index = 0;
                  for (const element of document.querySelectorAll('body *')) {
                    if ([...element.childNodes].some(node => node.nodeType === Node.TEXT_NODE && node.textContent.trim())) {
                      element.setAttribute(attribute, String(index++));
                    }
                  }
                }""",
                probe_attribute,
            )
            session = page.context.new_cdp_session(page)
            session.send("DOM.enable")
            session.send("CSS.enable")
            root = session.send("DOM.getDocument", {"depth": -1, "pierce": True})["root"]["nodeId"]
            node_ids = session.send("DOM.querySelectorAll", {
                "nodeId": root, "selector": f"[{probe_attribute}]",
            }).get("nodeIds", [])
            truncated = len(node_ids) > 512
            aggregated: dict[tuple[str, bool], int] = {}
            for node_id in node_ids[:512]:
                response = session.send("CSS.getPlatformFontsForNode", {"nodeId": node_id})
                for item in response.get("fonts", []):
                    key = (str(item.get("familyName") or ""), bool(item.get("isCustomFont")))
                    aggregated[key] = aggregated.get(key, 0) + int(item.get("glyphCount") or 0)
            return [
                {"family": family, "custom": custom, "glyphCount": glyph_count}
                for (family, custom), glyph_count in sorted(aggregated.items()) if family
            ], truncated
        except Exception:
            return [], False
        finally:
            try:
                page.evaluate(
                    "attribute => document.querySelectorAll(`[${attribute}]`).forEach(element => element.removeAttribute(attribute))",
                    probe_attribute,
                )
            except Exception:
                pass
            if session is not None:
                try:
                    session.detach()
                except Exception:
                    pass

    def record_page_environment(self, profile: str, page: Any) -> None:
        observation = page.evaluate(_FONT_OBSERVATION_JS)
        used_fonts, truncated = self._platform_fonts(page)
        profile_value = self._profiles.setdefault(profile, {
            **PROFILE_CONFIGS[profile],
            "locale": observation.get("locale"),
            "timezoneId": observation.get("timezoneId"),
            "observations": 0,
            "fontFaces": [],
            "computedFamilies": [],
            "usedPlatformFonts": [],
            "platformFontProbeTruncated": False,
        })
        profile_value["observations"] += 1
        profile_value["fontFaces"] = _merge_dict_entries(
            profile_value["fontFaces"], observation.get("fontFaces", []),
        )
        profile_value["computedFamilies"] = sorted(set(
            profile_value["computedFamilies"] + list(observation.get("computedFamilies", []))
        ))
        profile_value["usedPlatformFonts"] = _merge_platform_fonts(
            profile_value["usedPlatformFonts"], used_fonts,
        )
        profile_value["platformFontProbeTruncated"] = (
            profile_value["platformFontProbeTruncated"] or truncated
        )

    def record_embedded_fonts(self, document: dict[str, Any]) -> None:
        assets = document.get("assets", {}) if isinstance(document.get("assets"), dict) else {}
        entries: list[dict[str, str]] = []
        for font in document.get("fonts", []) if isinstance(document.get("fonts"), list) else []:
            if not isinstance(font, dict) or not isinstance(font.get("asset"), str):
                continue
            asset_id = font["asset"]
            payload = assets.get(asset_id)
            if isinstance(payload, str):
                entries.append({
                    "assetId": asset_id,
                    "family": str(font.get("family") or ""),
                    "digest": "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                })
        self._embedded_fonts = sorted(entries, key=lambda item: (item["assetId"], item["family"]))

    def profile_digest(self, profile: str) -> str:
        value = self._profiles.get(profile)
        if value is None:
            raise BrowserCheckError(f"Browser profile has no observation: {profile}")
        cache_profile = {
            **value,
            # Glyph counts describe deck content, not the renderer environment.
            # Keep them in diagnostics but exclude them from incremental keys.
            "usedPlatformFonts": [
                {"family": item["family"], "custom": item["custom"]}
                for item in value.get("usedPlatformFonts", [])
            ],
        }
        cache_profile.pop("observations", None)
        return canonical_digest({
            "playwrightVersion": _playwright_version(),
            "chromiumVersion": self.browser.version if self.browser is not None else "closed",
            "platform": _platform_value(),
            "profile": cache_profile,
            "embeddedFonts": self._embedded_fonts,
        })

    def report(self) -> dict[str, Any]:
        font_profiles_for_digest = {
            name: {
                "fontFaces": value["fontFaces"],
                "computedFamilies": value["computedFamilies"],
                "usedPlatformFonts": [
                    {"family": item["family"], "custom": item["custom"]}
                    for item in value["usedPlatformFonts"]
                ],
                "platformFontProbeTruncated": value["platformFontProbeTruncated"],
            }
            for name, value in sorted(self._profiles.items())
        }
        fonts_payload = {
            "format": FONT_ENVIRONMENT_FORMAT,
            "coverage": "used-platform-fonts-and-embedded-assets",
            "profiles": {
                name: {
                    "fontFaces": value["fontFaces"],
                    "computedFamilies": value["computedFamilies"],
                    "usedPlatformFonts": value["usedPlatformFonts"],
                    "platformFontProbeTruncated": value["platformFontProbeTruncated"],
                }
                for name, value in sorted(self._profiles.items())
            },
            "embeddedAssets": self._embedded_fonts,
        }
        fonts_payload["digest"] = canonical_digest({
            "format": FONT_ENVIRONMENT_FORMAT,
            "coverage": fonts_payload["coverage"],
            "profiles": font_profiles_for_digest,
            "embeddedAssets": self._embedded_fonts,
        })
        environment = {
            "playwrightVersion": _playwright_version(),
            "browser": {
                "name": "chromium",
                "version": self.browser.version if self.browser is not None else "closed",
                "headless": True,
                "distribution": self.browser_label,
            },
            "platform": _platform_value(),
            "profiles": {name: value for name, value in sorted(self._profiles.items())},
            "networkPolicy": {
                "format": NETWORK_POLICY_FORMAT,
                "allowedSchemes": ["file", "data", "blob", "about"],
                "loopbackHttpAllowed": True,
                "blockedRequests": sorted(
                    {tuple(sorted(item.items())) for item in self._blocked_requests}
                ),
            },
            "renderPolicy": {
                "reducedMotion": "reduce",
                "animations": "disabled-before-page-scripts",
                "fontReady": True,
                "imageDecode": True,
                "animationFramesAfterReady": 2,
                "colorScheme": "light",
            },
            "fonts": fonts_payload,
        }
        # Convert the privacy-safe tuple representation back to JSON objects.
        environment["networkPolicy"]["blockedRequests"] = [
            dict(item) for item in environment["networkPolicy"]["blockedRequests"]
        ]
        environment_for_digest = {
            **environment,
            "profiles": {
                name: {
                    **value,
                    "usedPlatformFonts": [
                        {"family": item["family"], "custom": item["custom"]}
                        for item in value["usedPlatformFonts"]
                    ],
                }
                for name, value in environment["profiles"].items()
            },
            "networkPolicy": {
                key: value for key, value in environment["networkPolicy"].items()
                if key != "blockedRequests"
            },
            "fonts": {
                "format": fonts_payload["format"],
                "coverage": fonts_payload["coverage"],
                "digest": fonts_payload["digest"],
                "embeddedAssets": fonts_payload["embeddedAssets"],
            },
        }
        for value in environment_for_digest["profiles"].values():
            value.pop("observations", None)
        return {
            "format": ENVIRONMENT_FORMAT,
            "environmentDigest": canonical_digest(environment_for_digest),
            "browserEnvironment": environment,
        }


def _platform_value() -> dict[str, str]:
    return {
        "system": platform.system(),
        "release": platform.release(),
        "architecture": platform.machine(),
    }


def _merge_dict_entries(current: list[dict[str, Any]], additions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    values = {_canonical_json(item): item for item in current if isinstance(item, dict)}
    for item in additions:
        if isinstance(item, dict):
            values[_canonical_json(item)] = item
    return [values[key] for key in sorted(values)]


def _merge_platform_fonts(current: list[dict[str, Any]], additions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    values: dict[tuple[str, bool], int] = {}
    for item in current + additions:
        key = (str(item.get("family") or ""), bool(item.get("custom")))
        values[key] = max(values.get(key, 0), int(item.get("glyphCount") or 0))
    return [
        {"family": family, "custom": custom, "glyphCount": glyph_count}
        for (family, custom), glyph_count in sorted(values.items()) if family
    ]
