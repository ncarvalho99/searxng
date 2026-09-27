// SPDX-License-Identifier: AGPL-3.0-or-later

import renderMathInElement from "katex/contrib/auto-render";
import "katex/dist/katex.min.css";
import { marked } from "marked";
import { Plugin } from "../Plugin.ts";

interface QuickAnswerConfig {
  systemPrompt: string;
  userPrompt: string;
  provider: string;
  model: string;
  referenceMap: Record<string, [string, string]>;
  queryHash: string;
}

declare global {
  interface Window {
    quickAnswerConfig?: QuickAnswerConfig;
  }
}

export default class QuickAnswer extends Plugin {
  public constructor() {
    super("quick_answer");
  }

  protected async run(): Promise<unknown> {
    const cardEl = document.getElementById("quick-answer-card");
    const bodyEl = document.getElementById("quick-answer-body");
    const textEl = document.getElementById("quick-answer-text") || bodyEl;
    const loadingEl = document.getElementById("quick-answer-loading");
    const refContainerEl = document.getElementById("quick-answer-references");
    const refListEl = document.getElementById("quick-answer-references-list");
    const copyBtnEl = document.getElementById("quick-answer-copy-btn");

    const config = window.quickAnswerConfig;
    if (!cardEl || !bodyEl || !loadingEl || !refContainerEl || !refListEl || !config || !textEl) {
      return false;
    }

    const safeConfig = config;
    const safeRefList = refListEl;
    const safeTextEl = textEl;

    // Initialize sources chips from referenceMap so sources are always visible
    const sourceKeys = Object.keys(safeConfig.referenceMap);
    if (sourceKeys.length > 0) {
      refContainerEl.style.display = "block";
      safeRefList.innerHTML = "";
      for (const key of sourceKeys) {
        const source = safeConfig.referenceMap[key];
        if (!source) continue;
        const [url, title] = source;
        const displayIndex = Number(key) + 1;

        let host = "";
        try {
          host = new URL(url).hostname.replace(/^www\./, "");
        } catch {
          host = url;
        }

        const chip = document.createElement("a");
        chip.href = url;
        chip.className = "quick-answer-source-chip";
        chip.target = "_blank";
        chip.rel = "noopener noreferrer";
        chip.title = title || url;
        chip.innerHTML = `<span class="quick-answer-chip-index">${displayIndex}</span><span class="quick-answer-chip-host">${escapeHtml(host)}</span>`;
        safeRefList.appendChild(chip);
      }
    }

    // Configure marked
    marked.setOptions({
      gfm: true,
      breaks: true
    });

    const mathExtension = {
      name: "math",
      level: "block" as const,
      start(src: string) {
        return src.match(/\$\$/)?.index;
      },
      tokenizer(src: string) {
        const rule = /^\$\$([\s\S]+?)\$\$/;
        const match = rule.exec(src);
        if (match && typeof match[0] === "string" && typeof match[1] === "string") {
          return {
            type: "math",
            raw: match[0],
            text: match[1].trim()
          };
        }
        return undefined;
      },
      renderer(token: { text: string }) {
        return `$$${token.text}$$`;
      }
    };

    const inlineMathExtension = {
      name: "inlineMath",
      level: "inline" as const,
      start(src: string) {
        return src.match(/\$/)?.index;
      },
      tokenizer(src: string) {
        const rule = /^\$([^$\n]+?)\$/;
        const match = rule.exec(src);
        if (match && typeof match[0] === "string" && typeof match[1] === "string") {
          return {
            type: "inlineMath",
            raw: match[0],
            text: match[1].trim()
          };
        }
        return undefined;
      },
      renderer(token: { text: string }) {
        return `$${token.text}$`;
      }
    };

    marked.use({ extensions: [mathExtension, inlineMathExtension] });

    function escapeHtml(unsafe: string): string {
      return unsafe.replace(/[&<>"']/g, (m) => {
        switch (m) {
          case "&":
            return "&amp;";
          case "<":
            return "&lt;";
          case ">":
            return "&gt;";
          case '"':
            return "&quot;";
          case "'":
            return "&#039;";
          default:
            return m;
        }
      });
    }

    function replaceCitations(text: string): string {
      return text.replace(/[【\[]([\d\s,;]+)[】\]]/g, (match, inner) => {
        const nums = inner.match(/\d+/g);
        if (!nums || nums.length === 0) return match;

        const links: string[] = [];
        for (const numStr of nums) {
          const num = Number(numStr);
          // Look up 1-based index (e.g. 1 -> key "0") or 0-based index
          let source = safeConfig.referenceMap[String(num - 1)];
          let displayNum = num;
          if (!source) {
            source = safeConfig.referenceMap[numStr];
            displayNum = num + 1;
          }
          if (!source) continue;
          const [url, title] = source;
          const escapedTitle = escapeHtml(title || url);
          links.push(
            `<a href="${url}" class="quick-answer-inline-ref" target="_blank" rel="noopener noreferrer" title="${escapedTitle}">[${displayNum}]</a>`
          );
        }
        return links.length > 0 ? links.join(" ") : match;
      });
    }

    try {
      const response = await fetch("/quick_answer", {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          system: safeConfig.systemPrompt,
          user: safeConfig.userPrompt,
          provider: safeConfig.provider,
          model: safeConfig.model,
          queryHash: safeConfig.queryHash
        })
      });

      if (!response.ok || !response.body) {
        const errText = await response.text().catch(() => "Erro na comunicação");
        loadingEl.style.display = "none";
        bodyEl.innerHTML = `<p class="quick-answer-error" style="color: #ef4444;">⚠️ ${escapeHtml(errText)}</p>`;
        return false;
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let accumulatedText = "";
      let hasContent = false;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        const chunk = decoder.decode(value, { stream: true });
        accumulatedText += chunk;

        if (!hasContent && accumulatedText.trim().length > 0) {
          hasContent = true;
          loadingEl.style.display = "none";
        }

        const processed = replaceCitations(accumulatedText);
        safeTextEl.innerHTML = marked.parse(processed) as string;

        renderMathInElement(safeTextEl, {
          delimiters: [
            { left: "$$", right: "$$", display: true },
            { left: "$", right: "$", display: false }
          ],
          throwOnError: false
        });
      }

      // Final render pass
      const finalProcessed = replaceCitations(accumulatedText);
      safeTextEl.innerHTML = marked.parse(finalProcessed) as string;

      renderMathInElement(safeTextEl, {
        delimiters: [
          { left: "$$", right: "$$", display: true },
          { left: "$", right: "$", display: false }
        ],
        throwOnError: false
      });

      cardEl.setAttribute("data-status", "ready");

      if (sourceKeys.length > 0) {
        refContainerEl.style.display = "block";
      }

      if (copyBtnEl && accumulatedText.trim().length > 0) {
        copyBtnEl.style.display = "inline-flex";
        copyBtnEl.addEventListener("click", () => {
          void navigator.clipboard.writeText(safeTextEl.innerText).then(() => {
            const copyLabel = copyBtnEl.querySelector(".quick-answer-copy-label");
            if (copyLabel) {
              const originalText = copyLabel.textContent;
              copyLabel.textContent = "Copiado!";
              setTimeout(() => {
                copyLabel.textContent = originalText;
              }, 2000);
            }
          });
        });
      }
    } catch (err: unknown) {
      console.error("QuickAnswer error:", err);
      loadingEl.style.display = "none";
      const message = err instanceof Error ? err.message : String(err);
      safeTextEl.innerHTML = `<p class="quick-answer-error" style="color: #ef4444;">⚠️ Erro ao consultar IA: ${escapeHtml(message)}</p>`;
    }

    return true;
  }

  protected async post(_result: unknown): Promise<void> {
    // Post execution
  }
}
