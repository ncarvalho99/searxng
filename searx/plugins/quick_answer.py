# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=missing-module-docstring, missing-class-docstring
import hashlib
import html
import json
import os
import typing as t
from datetime import datetime

from flask_babel import gettext
from searx._settings import get_setting
from searx.plugins import Plugin, PluginInfo

if t.TYPE_CHECKING:
    from searx.extended_types import SXNG_Request
    from searx.plugins import PluginCfg
    from searx.search import SearchWithPlugins


class SXNGPlugin(Plugin):
    id = "quick_answer"
    default_on = True

    def __init__(self, plg_cfg: "PluginCfg") -> None:
        super().__init__(plg_cfg)

        self.info = PluginInfo(
            id=self.id,
            name=gettext("Quick Answer"),
            description=gettext(
                "Respostas diretas geradas por IA sintetizando os resultados da busca ao terminar consultas com '?'"
            ),
            examples=["O que é Proxmox?", "Como funciona a fotossíntese?"],
            preference_section="general",
        )

    def get_sys_prompt(self) -> str:
        now = datetime.now()
        return f"""The current date is {now:%Y-%m-%d}.

You are an expert search assistant providing accurate, direct, well-formatted answers based on search results.

You ALWAYS follow these guidelines:
- Use markdown formatting to enhance clarity and readability.
- If you need to include mathematical expressions, use LaTeX format.
- Delimit inline mathematical expressions with '$', for example: $y = mx + b$.
- Delimit block mathematical expressions with '$$', for example: $$F = ma$$.
- Format code and commands as markdown code blocks with the language tag.
- DO NOT repeat or rephrase the query as a header before beginning your response.
- DO NOT include URLs or raw links directly in your response text.
- Enclose currency and price values in '**', for example: **$5.99**.

CITATION GUIDELINES:
1. Use the provided search results (<available_information>) to inform your answer.
2. Provide inline citations by placing the citation index delimited by 【 and 】 at the end of the sentence or claim, example: "This is a statement【1】."
3. When citing multiple sources for one statement, use separate delimiters, example: "This is supported by multiple sources【1】【2】."
4. Use citations relevant to the query; do not create long chains of citations.
5. DO NOT create an aggregate bibliography or list of links at the end of the response; the interface renders this automatically based on citation indices.
6. DO NOT put citations inside or around code blocks.
7. Always format citations in plaintext 【n】, never in markdown link syntax.
8. Be concise, objective, and synthesize the information in your own words.
"""

    def format_sources(self, sources: list[dict[str, t.Any]]) -> str:
        ret = ["<available_information>"]
        for pos, source in enumerate(sources):
            ret.append("<datum>")
            ret.append(f'<citation index="{pos}">')
            ret.append(f"<source>\n{source.get('url', '')}\n</source>")
            ret.append(f"<title>\n{source.get('title', '')}\n</title>")
            ret.append(f"<content>\n{source.get('content', '')}\n</content>")
            ret.append("</citation>")
            ret.append("</datum>")
        ret.append("</available_information>")
        return "\n".join(ret)

    def post_search(self, request: "SXNG_Request", search: "SearchWithPlugins") -> None:
        query = search.search_query
        raw_query = query.query.strip()
        if query.pageno > 1 or not raw_query.endswith("?"):
            return

        # Check user preference
        if not request.preferences.get_value("quick_answer_enable"):
            return

        qa_cfg = get_setting("quick_answer") or {}
        if not qa_cfg.get("active", True):
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

        provider_name = provider_info.get("name", selected_provider_id)

        # Resolve model
        selected_model = (
            request.preferences.get_value("quick_answer_model")
            or provider_info.get("default_model")
            or qa_cfg.get("default_model", "auto/best-chat")
        )

        sources = search.result_container.get_ordered_results()
        if not sources:
            return

        formatted_sources = self.format_sources(sources)
        user_prompt = f"{formatted_sources}\n\nUser query: {raw_query}"
        system_prompt = self.get_sys_prompt()

        reference_map = {
            str(i): [source.get("url", ""), source.get("title", "")]
            for i, source in enumerate(sources)
        }

        query_hash = hashlib.sha256(
            f"{selected_provider_id}:{selected_model}:{raw_query}".encode("utf-8")
        ).hexdigest()

        search.result_container.infoboxes.append(
            {
                "infobox": gettext("Quick Answer"),
                "id": "quick_answer",
                "content": f"""
            <div class="quick-answer-card" id="quick-answer-card" data-status="pending">
              <div class="quick-answer-header">
                <div class="quick-answer-title-group">
                  <span class="quick-answer-badge">AI</span>
                  <span class="quick-answer-provider-tag" title="Provedor">{html.escape(provider_name)}</span>
                  <span class="quick-answer-model-tag" title="Modelo">{html.escape(selected_model)}</span>
                </div>
                <div class="quick-answer-actions">
                  <button type="button" class="quick-answer-copy-btn" id="quick-answer-copy-btn" title="Copiar resposta" style="display:none;">
                    <span class="quick-answer-copy-icon"></span>
                    <span class="quick-answer-copy-label">Copiar</span>
                  </button>
                </div>
              </div>
              <div class="quick-answer-body markdown-content" id="quick-answer-body">
                <div class="quick-answer-loading" id="quick-answer-loading">
                  <span class="quick-answer-spinner"></span>
                  <span class="quick-answer-loading-text">Consultando IA e sintetizando fontes...</span>
                </div>
              </div>
              <div class="quick-answer-references" id="quick-answer-references" style="display:none;">
                <h4 class="quick-answer-references-title">Fontes consultadas</h4>
                <ol class="quick-answer-references-list" id="quick-answer-references-list"></ol>
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
