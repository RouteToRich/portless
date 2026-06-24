from __future__ import annotations

from langchain_core.documents import Document

from .config import settings


def build_augmented_prompt(question: str, chunks: list[Document]) -> str:
    """Build the grounded prompt sent to the generation step.

    Implementation:
        The function formats each selected chunk with a citation number, title,
        source path, section, page number, chunk index, and chunk text. It then
        wraps the context with instructions that require evidence-grounded
        answers and citations.

    Usage:
        The query graph calls this in the `augment` node after retrieval,
        reranking, and knee-point selection have chosen final chunks.

    How it helps other functions:
        `generate_answer` uses this prompt when OpenAI generation is enabled.
        The same formatting also documents exactly what evidence was made
        available to the model.
    """
    context_blocks = []
    for idx, chunk in enumerate(chunks, start=1):
        source = chunk.metadata.get("source", "unknown")
        title = chunk.metadata.get("title", "untitled")
        section = chunk.metadata.get("section", "unknown")
        page_number = chunk.metadata.get("page_number", "n/a")
        chunk_index = chunk.metadata.get("chunk_index", "n/a")
        context_blocks.append(
            f"[{idx}] title={title}; section={section}; page={page_number}; "
            f"source={source}; chunk={chunk_index}\n{chunk.page_content}"
        )

    context = "\n\n---\n\n".join(context_blocks)
    return f"""You are a grounded RAG assistant.

Answer the question using only the context below.
If the context is insufficient, say what evidence is missing.
Include citations like [1] or [2] for important claims.

Question:
{question}

Retrieved context:
{context}

Answer:
"""


def generate_answer(question: str, chunks: list[Document]) -> str:
    """Generate the final RAG answer from the selected evidence chunks.

    Implementation:
        The function always builds the augmented prompt first. If
        `USE_OPENAI_LLM=true`, it sends that prompt to the configured OpenAI chat
        model with temperature zero. Otherwise it returns a local extractive
        answer so the project remains runnable without credentials.

    Usage:
        The query graph calls this in its final `generate` node.

    How it helps other functions:
        It is the terminal step that turns retrieval output into user-facing
        text. It depends on upstream functions to provide relevant, deduplicated,
        and properly cited chunks.
    """
    prompt = build_augmented_prompt(question, chunks)
    if settings.use_openai_llm:
        from langchain_openai import ChatOpenAI

        llm = ChatOpenAI(model=settings.openai_chat_model, temperature=0)
        return str(llm.invoke(prompt).content)
    return _local_extractive_answer(question, chunks)


def _local_extractive_answer(question: str, chunks: list[Document]) -> str:
    """Return a simple evidence summary when no hosted LLM is configured.

    Implementation:
        If no chunks are available, it clearly says there is not enough evidence.
        Otherwise it creates short snippets from selected chunks and includes
        source metadata beside each citation number.

    Usage:
        `generate_answer` calls this in local practice mode.

    How it helps other functions:
        It lets you validate ingestion, retrieval, reranking, and augmentation
        before introducing LLM credentials or generation costs.
    """
    if not chunks:
        return "I do not have enough retrieved evidence to answer this question."

    lines = [
        "Local grounded answer:",
        "",
        f"Question: {question}",
        "",
        "Most relevant evidence:",
    ]
    for idx, chunk in enumerate(chunks, start=1):
        source = chunk.metadata.get("file_name") or chunk.metadata.get("source", "unknown")
        section = chunk.metadata.get("section", "unknown")
        page_number = chunk.metadata.get("page_number", "n/a")
        snippet = " ".join(chunk.page_content.split())[:500]
        lines.append(f"- [{idx}] {snippet} (source: {source}, section: {section}, page: {page_number})")

    lines.extend(
        [
            "",
            "Summary:",
            "The answer should be based on the cited chunks above. Enable USE_OPENAI_LLM=true for a fluent LLM-generated response.",
        ]
    )
    return "\n".join(lines)
