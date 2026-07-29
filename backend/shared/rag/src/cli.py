from __future__ import annotations

import argparse
from typing import Any

from .config import settings
from .graph import build_ingestion_graph, build_query_graph


def main() -> None:
    """Run the command-line interface for the modular RAG project.

    Implementation:
        The CLI exposes two commands. `ingest` builds and invokes the ingestion
        LangGraph with input paths and a chunking strategy, writing both vector
        and graph stores. `ask` builds and invokes the query LangGraph with a
        user question, optional metadata filters, and optional user/team
        authorization context.

    Usage:
        Run `practice-rag ingest --path ./data --strategy recursive` to populate
        the MongoDB vector store, then run `practice-rag ask "your question"` to
        retrieve and answer from the indexed documents. Use
        `--filter section=Release` or `--filter page_number=3` to scope
        retrieval. Use `--user` and `--team` to apply authorization metadata.

    How it helps other functions:
        This function is the user-facing entry point that connects terminal
        arguments to `build_ingestion_graph`, `build_query_graph`, vector
        storage, graph storage, metadata filtering, authorization filtering,
        citation validation, and evaluation metrics.
    """
    parser = argparse.ArgumentParser(description="Practice LangGraph RAG system")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest_parser = subparsers.add_parser("ingest", help="Parse, chunk, embed, and insert documents")
    ingest_parser.add_argument("--path", action="append", default=None, help="File or folder to ingest")
    ingest_parser.add_argument(
        "--strategy",
        choices=["recursive", "semantic"],
        default="recursive",
        help="Chunking strategy",
    )

    ask_parser = subparsers.add_parser("ask", help="Run retrieval, reranking, augmentation, and generation")
    ask_parser.add_argument("question", help="Question to answer from the RAG index")
    ask_parser.add_argument(
        "--filter",
        action="append",
        default=None,
        help="Exact metadata filter in key=value form. Can be repeated.",
    )
    ask_parser.add_argument("--user", default=None, help="User ID for authorization filtering")
    ask_parser.add_argument("--team", default=None, help="Team ID for authorization filtering")

    args = parser.parse_args()

    if args.command == "ingest":
        if not args.path:
            print("Error: provide at least one --path argument.")
            raise SystemExit(1)
        import asyncio
        graph = build_ingestion_graph()
        result = asyncio.run(
            graph.ainvoke(
                {
                    "input_paths": args.path,
                    "chunk_strategy": args.strategy,
                }
            )
        )
        print(f"Inserted vector chunks: {result.get('inserted_count', 0)}")
        print(f"Inserted graph chunks: {result.get('graph_inserted_count', 0)}")
        return

    if args.command == "ask":
        import asyncio
        graph = build_query_graph()
        result = asyncio.run(
            graph.ainvoke(
                {
                    "question": args.question,
                    "metadata_filter": parse_metadata_filters(args.filter or []),
                    "user_id": args.user,
                    "team_id": args.team,
                }
            )
        )
        print(result["answer"])
        print("\nCitation validation:", result.get("citation_validation"))
        print("Evaluation metrics:", result.get("evaluation_metrics"))


def parse_metadata_filters(filters: list[str]) -> dict[str, Any]:
    """Parse CLI metadata filters from `key=value` strings.

    Implementation:
        The function splits each filter at the first equals sign and converts
        simple scalar values into booleans or integers when possible. Empty input
        returns an empty dictionary, which the graph treats as no filter.

    Usage:
        `main` calls this for the `ask --filter key=value` options before
        invoking the query graph.

    How it helps other functions:
        Parsed filters flow into `semantic_search` and `keyword_search`, letting
        retrieval narrow candidates by metadata such as `section`, `file_type`,
        or `page_number`.
    """
    parsed: dict[str, Any] = {}
    for item in filters:
        if "=" not in item:
            raise ValueError(f"Invalid filter '{item}'. Use key=value.")
        key, value = item.split("=", 1)
        parsed[key.strip()] = _coerce_filter_value(value.strip())
    return parsed


def _coerce_filter_value(value: str) -> Any:
    """Convert a CLI filter value into a simple metadata scalar.

    Implementation:
        The function recognizes booleans and integers, otherwise returns the
        original string. Metadata filters work best with simple scalar values,
        since they are matched exactly against stored metadata.

    Usage:
        `parse_metadata_filters` calls this for every filter value.

    How it helps other functions:
        It lets users write `--filter page_number=3` and match integer metadata
        rather than the string value `"3"`.
    """
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if value.isdigit():
        return int(value)
    return value


if __name__ == "__main__":
    main()
