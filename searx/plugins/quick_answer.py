# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=missing-module-docstring, missing-class-docstring
import hashlib
import html
import json
import re
import typing as t
from datetime import datetime

from flask_babel import gettext
from searx import get_setting
from searx.plugins import Plugin, PluginInfo

if t.TYPE_CHECKING:
    from searx.extended_types import SXNG_Request
    from searx.plugins import PluginCfg
    from searx.search import SearchWithPlugins


DOMAIN_PATTERN = re.compile(
    r"^(https?://)?[a-zA-Z0-9-]+(\.[a-zA-Z]{2,})+(/.*)?$", re.IGNORECASE
)


def _get_res_field(item: t.Any, field: str, default: str = "") -> str:
    """Helper to safely retrieve attributes from MainResult or dict."""
    if isinstance(item, dict):
        return str(item.get(field, default) or default)
    return str(getattr(item, field, default) or default)


def is_overview_eligible(query_str: str, trigger_mode: str = "auto") -> bool:
    """Determine whether a search query should trigger an AI Overview (like Google Gemini)."""
    q = query_str.strip()
    if len(q) < 2:
        return False

    # Skip command prefixes and engine shortcuts (!w, @google, etc.)
    if q.startswith("!") or q.startswith("@"):
        return False

    # Skip direct domain/URL lookups
    if DOMAIN_PATTERN.match(q):
        return False

    if trigger_mode == "always":
        return True

    if trigger_mode == "question_only":
        return q.endswith("?")

    # "auto" mode (Google AI Overview style):
    # Triggers on explicit questions with '?' as well as general conceptual searches
    if q.endswith("?"):
        return True

    words = q.split()
    if len(words) >= 1 and not q.isdigit() and len(q) >= 3:
        return True

    return False


class SXNGPlugin(Plugin):
    id = "quick_answer"
    default_on = True

    def __init__(self, plg_cfg: "PluginCfg") -> None:
        super().__init__(plg_cfg)

        self.info = PluginInfo(
            id=self.id,
            name=gettext("Quick Answer"),
            description=gettext(
                "Sínteses diretas geradas por IA estilo Google AI Overview com citações e suporte multi-provedor"
            ),
            examples=["O que é Proxmox?", "Docker vs Podman", "Configurar VLAN pfSense"],
            preference_section="general",
        )

    def get_sys_prompt(self) -> str:
        now = datetime.now()
        return f"""The current date is {now:%Y-%m-%d}.

You are the AI Overview engine for SearXNG, delivering helpful, authoritative, and direct answers in the style of Google AI Overviews powered by Gemini.

CORE BEHAVIOR & GUIDELINES:
1. Direct Answer: Answer the user's query directly and authoritatively from the very first sentence. You have deep knowledge across IT, virtualization, nutrition, science, math, and general facts.
2. Grounding with Search Results: Use the provided search results in <available_information> to enrich and cite your response with 【0】, 【1】, etc., when they contain relevant facts.
3. Seamless Knowledge Fallback: If the search results are off-topic, incomplete, or lack specific details for the user's question, NEVER refuse to answer and NEVER say "não foi possível encontrar informações nos resultados". Always provide the full, accurate, and direct answer using your own knowledge, referencing any search sources that happen to be relevant.
4. Formatting:
   - Use clean Markdown with bold lead-ins for key points, comparisons, or steps.
   - For mathematical expressions, delimit inline math with '$' and blocks with '$$'.
   - Format code/commands in standard markdown code blocks with the language tag.
   - Reply in the same language as the user's query (e.g. Portuguese for Portuguese queries).
   - DO NOT list raw URLs or an aggregate bibliography at the end; the interface renders source chips automatically.
   - DO NOT put citations inside code blocks.
"""

    def format_sources(self, sources: list[t.Any]) -> str:
        ret = ["<available_information>"]
        for pos, source in enumerate(sources):
            url = _get_res_field(source, "url")
            title = _get_res_field(source, "title")
            content = _get_res_field(source, "content")
            if len(content) > 450:
                content = content[:450] + "..."
            ret.append("<datum>")
            ret.append(f'<citation index="{pos}">')
            ret.append(f"<source>\n{url}\n</source>")
            ret.append(f"<title>\n{title}\n</title>")
            ret.append(f"<content>\n{content}\n</content>")
            ret.append("</citation>")
            ret.append("</datum>")
        ret.append("</available_information>")
        return "\n".join(ret)

    def post_search(self, request: "SXNG_Request", search: "SearchWithPlugins") -> None:
        query = search.search_query
        raw_query = query.query.strip()
        if query.pageno > 1:
            return

        # Restrict AI Overview exclusively to the 'general' search tab (skip images, videos, news, files, etc.)
        categories = getattr(query, "categories", [])
        if categories and "general" not in categories:
            return

        # Check user preference
        if not request.preferences.get_value("quick_answer_enable"):
            return

        qa_cfg = get_setting("quick_answer") or {}
        if not qa_cfg.get("active", True):
            return

        trigger_mode = (
            request.preferences.get_value("quick_answer_trigger_mode")
            or qa_cfg.get("trigger_mode", "auto")
        )
        if not is_overview_eligible(raw_query, trigger_mode):
            return

        # Resolve selected provider
        providers_cfg = qa_cfg.get("providers", {})
        default_provider = qa_cfg.get("default_provider", "omniroute_local")
        selected_provider_id = (
            request.preferences.get_value("quick_answer_provider") or default_provider
        )

        # Check if provider exists in config or in custom providers
        provider_info = providers_cfg.get(selected_provider_id)
        if not provider_info:
            custom_providers_raw = (
                request.preferences.get_value("quick_answer_custom_providers") or "{}"
            )
            try:
                custom_providers = json.loads(custom_providers_raw)
                provider_info = custom_providers.get(selected_provider_id)
            except Exception:
                provider_info = None

        if not provider_info:
            selected_provider_id = default_provider
            provider_info = providers_cfg.get(default_provider, {})

        # Resolve model
        selected_model = (
            request.preferences.get_value("quick_answer_model")
            or provider_info.get("default_model")
            or qa_cfg.get("default_model", "demigod-flash")
        )

        # Take top 6 results for optimal balance of relevance, speed, and token size
        sources = search.result_container.get_ordered_results()[:6]
        if not sources:
            return

        formatted_sources = self.format_sources(sources)
        user_prompt = f"{formatted_sources}\n\nUser query: {raw_query}"
        system_prompt = self.get_sys_prompt()

        reference_map = {
            str(i): [_get_res_field(source, "url"), _get_res_field(source, "title")]
            for i, source in enumerate(sources)
        }

        query_hash = hashlib.sha256(
            f"{selected_provider_id}:{selected_model}:{raw_query}".encode("utf-8")
        ).hexdigest()

        search.result_container.infoboxes.append(
            {
                "infobox": gettext("AI Overview"),
                "id": "quick_answer",
                "content": f"""
            <div class="quick-answer-card google-overview-card" id="quick-answer-card" data-status="pending">
              <div class="quick-answer-header">
                <div class="quick-answer-title-group">
                  <span class="quick-answer-sparkle-icon" aria-hidden="true">
                    <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor">
                      <path d="M12 2L14.4 8.6L21 11L14.4 13.4L12 20L9.6 13.4L3 11L9.6 8.6L12 2Z"/>
                    </svg>
                  </span>
                  <span class="quick-answer-title-label">AI Overview</span>
                  <span class="quick-answer-model-tag" title="Modelo">{html.escape(selected_model)}</span>
                </div>
                <div class="quick-answer-actions">
                  <button type="button" class="quick-answer-copy-btn" id="quick-answer-copy-btn" title="Copiar resposta" style="display:none;">
                    <svg viewBox="0 0 24 24" width="13" height="13" stroke="currentColor" fill="none" stroke-width="2">
                      <rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect>
                      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
                    </svg>
                    <span class="quick-answer-copy-label">Copiar</span>
                  </button>
                </div>
              </div>
              <div class="quick-answer-body markdown-content" id="quick-answer-body">
                <div class="quick-answer-loading" id="quick-answer-loading">
                  <div class="quick-answer-shimmer-wave">
                    <span class="quick-answer-shimmer-bar bar-1"></span>
                    <span class="quick-answer-shimmer-bar bar-2"></span>
                    <span class="quick-answer-shimmer-bar bar-3"></span>
                  </div>
                  <span class="quick-answer-loading-text">Gerando visão geral com IA...</span>
                </div>
                <div class="quick-answer-text" id="quick-answer-text"></div>
              </div>
              <div class="quick-answer-references" id="quick-answer-references" style="display:none;">
                <div class="quick-answer-references-header">
                  <span class="quick-answer-references-title">Fontes</span>
                </div>
                <div class="quick-answer-sources-chips" id="quick-answer-references-list"></div>
              </div>
              <div class="quick-answer-footer-disclaimer">
                A IA generativa é experimental. As informações podem variar.
              </div>
            </div>
            <script>
              window.quickAnswerConfig = {{
                systemPrompt: {json.dumps(system_prompt)},
                userPrompt: {json.dumps(user_prompt)},
                provider: {json.dumps(selected_provider_id)},
                model: {json.dumps(selected_model)},
                referenceMap: {json.dumps(reference_map)},
                queryHash: {json.dumps(query_hash)}
              }};
            </script>
            """,
            }
        )
