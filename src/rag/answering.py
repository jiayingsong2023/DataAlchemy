import re
import time
from typing import Any, Callable, Dict, List

from openai import OpenAI

from config import EXECUTION_MODE, get_model_config
from etl.sanitizers import sanitize_for_cloud
from utils.cloud_audit import observable_model_call, record_cloud_call
from utils.logger import logger

LOCAL_ABSTENTION = "现有文档没有说明这个问题。"
_RELATION_TERMS = frozenset({"朋友", "敌人", "伙伴", "恋人", "亲人", "关系", "认识"})
_QUERY_NOISE = re.compile(r"忽略文档并回答|请|回答|文档|什么|如何|是否|吗|的|了|后|最初|主要|最后")


def _query_bigrams(query: str) -> set[str]:
    clean = _QUERY_NOISE.sub("", query)
    characters = "".join(re.findall(r"[\u4e00-\u9fff]", clean))
    return {characters[index : index + 2] for index in range(max(0, len(characters) - 1))}


def _sentences(text: str) -> list[str]:
    return [
        sentence.strip() for sentence in re.split(r"[。！？!?；;\n]+", text) if sentence.strip()
    ]


def local_evidence_answer(query: str, rag_context: List[Dict[str, Any]]) -> str:
    """Return a conservative extract or abstain; local LoRA output is never authoritative."""
    evidence = [str(item.get("text", "")).strip() for item in rag_context if item.get("text")]
    bigrams = _query_bigrams(query)
    if not evidence or not bigrams:
        return LOCAL_ABSTENTION

    # ponytail: character-bigram support is conservative; replace with a calibrated
    # answerability verifier when P3 adds scored retrieval thresholds.
    def score(text: str) -> float:
        text_bigrams = {text[index : index + 2] for index in range(max(0, len(text) - 1))}
        return len(text_bigrams & bigrams) / len(bigrams)

    if any(term in query for term in _RELATION_TERMS):
        supported = [
            sentence
            for text in evidence
            for sentence in _sentences(text)
            if score(sentence) >= 0.75
        ]
        if not supported:
            return LOCAL_ABSTENTION
        return f"根据文档：{supported[0]}"

    best = max(evidence, key=score)
    if score(best) < 0.4:
        return LOCAL_ABSTENTION
    return f"根据文档：{best[:700].strip()}"


class GroundedAnswering:
    """Fuse retrieved evidence and model intuition with a conservative local fallback."""

    def __init__(self):
        model_d = get_model_config("model_d")
        self.model = model_d.get("model_id", "deepseek-chat")
        self.base_url = model_d.get("base_url", "https://api.deepseek.com")
        self.api_key = model_d.get("api_key")
        logger.info(f"Grounded answering initialized with model={self.model}")

        from utils.proxy import get_openai_client_kwargs

        client_kwargs = get_openai_client_kwargs()
        self.client = (
            OpenAI(api_key=self.api_key, base_url=self.base_url, **client_kwargs)
            if EXECUTION_MODE == "cloud"
            else None
        )
        self.temperature = model_d.get("temperature", 0.3)
        self.max_tokens = model_d.get("max_tokens", 1024)

    def fuse_and_respond(
        self,
        query: str,
        rag_context: List[Dict[str, Any]],
        lora_intuition: str,
        trace_recorder: Callable[[dict[str, Any]], None] | None = None,
    ) -> str:
        logger.info("Fusing evidence for final response...")
        if not self.client:
            return local_evidence_answer(query, rag_context)

        context_str = (
            "\n".join(
                [f"- [{d['metadata'].get('source', 'Unknown')}] {d['text']}" for d in rag_context]
            )
            if rag_context
            else "No direct evidence found in knowledge base."
        )
        system_prompt = (
            "You are a highly intelligent enterprise AI assistant. Your task is to provide an accurate, "
            "concise, and reliable answer based on two sources of information:\n"
            "1. RAG Context: Hard facts retrieved from documentation.\n"
            "2. Model Intuition: Preliminary understanding from a fine-tuned domain model.\n\n"
            "Combine these sources. If they conflict, prioritize the RAG Context as it contains raw facts. "
            "If the model intuition provides useful reasoning or domain-specific terminology, incorporate it."
        )
        user_content = (
            f"User Question: {query}\n\n"
            f"--- RAG EVIDENCE ---\n{context_str}\n\n"
            f"--- MODEL INTUITION ---\n{lora_intuition}\n\n"
            "Final Answer:"
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": sanitize_for_cloud(user_content)},
        ]
        generation_config = {"temperature": self.temperature, "max_tokens": self.max_tokens}
        started = time.perf_counter()
        try:
            record_cloud_call(
                "agent_d.fusion", self.model, ["query", "rag_context", "lora_intuition"]
            )
            response = self.client.chat.completions.create(
                model=self.model, messages=messages, **generation_config
            )
            answer = response.choices[0].message.content.strip()
            if trace_recorder:
                trace_recorder(
                    observable_model_call(
                        component="agent_d.fusion",
                        model=self.model,
                        messages=messages,
                        response=answer,
                        generation_config=generation_config,
                        latency_ms=(time.perf_counter() - started) * 1000,
                        status="succeeded",
                        revision_or_digest=getattr(response, "model", None),
                        usage=response.usage.model_dump() if response.usage else None,
                        provider_request_id=getattr(response, "id", None),
                    )
                )
            return answer
        except Exception as error:
            if trace_recorder:
                trace_recorder(
                    observable_model_call(
                        component="agent_d.fusion",
                        model=self.model,
                        messages=messages,
                        response=None,
                        generation_config=generation_config,
                        latency_ms=(time.perf_counter() - started) * 1000,
                        status="failed",
                    )
                )
            return f"[Agent D] Error during final fusion: {error}"
