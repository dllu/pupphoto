"""Commons category discovery, graph traversal, and conservative refinement."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from upload_commons import CommonsApi

MAX_CATEGORY_INTERSECTION_PAIRS = 1
MAX_CATEGORY_INTERSECTION_RESULTS = 8


@dataclass
class CategoryNode:
    title: str
    parents: list[str]
    children: list[str]
    query_hits: list[str]
    source: str
    file_count: int
    subcategory_count: int


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _category_intersection_query(category_pairs: list[tuple[str, str]]) -> str:
    def quoted_category(category: str) -> str:
        value = category.removeprefix("Category:")
        return value.replace("\\", "\\\\").replace('"', '\\"')

    return " OR ".join(
        f'incategory:"{quoted_category(left)}" incategory:"{quoted_category(right)}"'
        for left, right in category_pairs
    )


def _category_title_supported_by_evidence(title: str, evidence: list[str]) -> bool:
    normalized_title = " ".join(
        re.findall(r"[^\W_]+", title.removeprefix("Category:").casefold())
    )
    normalized_evidence = " ".join(
        re.findall(r"[^\W_]+", " ".join(evidence).casefold())
    )
    return bool(normalized_title) and normalized_title in normalized_evidence


def _category_title_variants(query: str) -> list[str]:
    base = re.sub(r"\s+", " ", query.removeprefix("Category:").strip())
    if not base:
        return []
    variants = [
        base,
        base[:1].upper() + base[1:],
    ]
    return [f"Category:{variant}" for variant in _dedupe_preserve_order(variants)]


def _add_category_node(
    nodes: dict[str, CategoryNode],
    title: str,
    source: str,
    query_hit: str | None = None,
) -> CategoryNode:
    node = nodes.get(title)
    if node is None:
        node = CategoryNode(
            title=title,
            parents=[],
            children=[],
            query_hits=[],
            source=source,
            file_count=0,
            subcategory_count=0,
        )
        nodes[title] = node
    elif source not in node.source.split("+"):
        node.source = f"{node.source}+{source}"
    if query_hit:
        node.query_hits.append(query_hit)
    return node


def _build_category_graph(
    commons_api: CommonsApi,
    queries: list[str],
    limit_per_query: int,
    max_candidate_categories: int,
    max_parent_depth: int,
) -> dict[str, Any]:
    print("Building candidate Commons category graph...", flush=True)
    seed_nodes: dict[str, CategoryNode] = {}
    exact_query_hits_by_title: dict[str, list[str]] = {}
    queries = _dedupe_preserve_order(queries)
    for query in queries:
        for title in _category_title_variants(query):
            exact_query_hits_by_title.setdefault(title, []).append(query)

    exact_candidate_titles = _dedupe_preserve_order(list(exact_query_hits_by_title))
    resolved_queries: set[str] = set()
    for title in commons_api.existing_category_titles(exact_candidate_titles):
        for query in exact_query_hits_by_title.get(title, []):
            _add_category_node(seed_nodes, title, "exact", query)
            resolved_queries.add(query)
    # Exact facet titles are stronger seeds than broad full-text matches. Their
    # relatives are explored below; only unresolved queries need text search.
    for query in queries:
        if query in resolved_queries:
            continue
        for title in commons_api.search_categories(query, limit_per_query):
            _add_category_node(seed_nodes, title, "search", query)

    exact_titles = [
        title for title, node in seed_nodes.items() if "exact" in node.source.split("+")
    ]
    exact_title_set = set(exact_titles)
    other_titles = [title for title in seed_nodes if title not in exact_title_set]
    limited_titles = (exact_titles + other_titles)[:max_candidate_categories]
    if len(seed_nodes) > len(limited_titles):
        limited_set = set(limited_titles)
        dropped = [title for title in seed_nodes if title not in limited_set]
        print(
            "Dropping candidate categories due to max_candidate_categories: "
            + json.dumps(dropped, ensure_ascii=True),
            flush=True,
        )
    graph_nodes: dict[str, CategoryNode] = {
        title: seed_nodes[title] for title in limited_titles
    }
    frontier = list(graph_nodes)
    detailed_titles: set[str] = set()
    child_lookup_count = 0
    depth = 0
    while frontier and depth < max_parent_depth:
        details_map = commons_api.category_details(frontier)
        child_lookup_titles: list[str] = []
        for title in frontier:
            if child_lookup_count >= commons_api._MAX_CHILD_CATEGORY_LOOKUPS:
                break
            node = graph_nodes[title]
            if depth == 0 or "exact" in node.source.split("+"):
                child_lookup_titles.append(title)
                child_lookup_count += 1
        child_map = commons_api.category_subcategories(
            child_lookup_titles, max(limit_per_query, 1)
        )
        next_frontier: list[str] = []
        for title in frontier:
            details = details_map.get(title, {})
            parents = details.get("parents", [])
            children = child_map.get(title, [])
            graph_nodes[title].parents = _dedupe_preserve_order(
                graph_nodes[title].parents + parents
            )
            graph_nodes[title].children = _dedupe_preserve_order(
                graph_nodes[title].children + children
            )
            graph_nodes[title].file_count = int(details.get("file_count", 0))
            graph_nodes[title].subcategory_count = int(
                details.get("subcategory_count", 0)
            )
            detailed_titles.add(title)
            for parent in parents:
                if parent not in graph_nodes:
                    _add_category_node(graph_nodes, parent, "ancestor")
                    if parent not in next_frontier:
                        next_frontier.append(parent)
            for child in children:
                child_node = _add_category_node(graph_nodes, child, "child")
                child_node.parents = _dedupe_preserve_order(
                    child_node.parents + [title]
                )
                if child not in detailed_titles and child not in next_frontier:
                    next_frontier.append(child)
        frontier = next_frontier
        depth += 1
    remaining_titles = [title for title in graph_nodes if title not in detailed_titles]
    # Boundary nodes are still candidates. Fetch their parents as well as counts
    # so ancestor cleanup can see relationships without expanding the search.
    details_map = commons_api.category_details(remaining_titles)
    for title in remaining_titles:
        node = graph_nodes[title]
        details = details_map.get(title, {})
        node.parents = _dedupe_preserve_order(node.parents + details.get("parents", []))
        node.file_count = int(details.get("file_count", 0))
        node.subcategory_count = int(details.get("subcategory_count", 0))
    return {
        "nodes": {
            title: {
                "parents": node.parents,
                "children": node.children,
                "query_hits": _dedupe_preserve_order(node.query_hits),
                "source": node.source,
                "file_count": node.file_count,
                "subcategory_count": node.subcategory_count,
            }
            for title, node in sorted(graph_nodes.items())
        }
    }


def _remove_ancestor_duplicates(
    selected_categories: list[str], graph: dict[str, Any]
) -> list[str]:
    node_map = graph.get("nodes", {})

    def ancestors_of(title: str) -> set[str]:
        stack = list(node_map.get(f"Category:{title}", {}).get("parents", []))
        seen: set[str] = set()
        while stack:
            parent = stack.pop()
            if parent in seen:
                continue
            seen.add(parent)
            stack.extend(node_map.get(parent, {}).get("parents", []))
        return {item.removeprefix("Category:") for item in seen}

    selected_ancestors: set[str] = set()
    for category in selected_categories:
        selected_ancestors.update(ancestors_of(category))
    return [
        category
        for category in selected_categories
        if category not in selected_ancestors
    ]


def _remove_empty_categories(
    selected_categories: list[str], graph: dict[str, Any]
) -> list[str]:
    node_map = graph.get("nodes", {})
    filtered: list[str] = []
    removed: list[str] = []
    for category in selected_categories:
        node = node_map.get(f"Category:{category}", {})
        if int(node.get("file_count", 0)) <= 0:
            removed.append(category)
            continue
        filtered.append(category)
    if removed:
        print(
            "Dropping zero-file topical categories: "
            + json.dumps(removed, ensure_ascii=True),
            flush=True,
        )
    return filtered


def _refine_category_intersections(
    commons_api: CommonsApi,
    selected_categories: list[str],
    graph: dict[str, Any],
    evidence: list[str],
    intersection_cache: (
        dict[tuple[tuple[str, str], ...], dict[str, dict[str, Any]]] | None
    ) = None,
) -> list[str]:
    if len(selected_categories) < 2:
        return selected_categories

    graph_nodes = graph.setdefault("nodes", {})
    ranked_pairs: list[tuple[int, int, int, tuple[str, str]]] = []
    pair_index = 0
    for left_index, left in enumerate(selected_categories):
        for right in selected_categories[left_index + 1 :]:
            left_title = f"Category:{left}"
            right_title = f"Category:{right}"
            left_parents = set(graph_nodes.get(left_title, {}).get("parents", []))
            right_parents = set(graph_nodes.get(right_title, {}).get("parents", []))
            left_tokens = set(re.findall(r"[^\W_]+", left.casefold()))
            right_tokens = set(re.findall(r"[^\W_]+", right.casefold()))
            ranked_pairs.append(
                (
                    len(left_parents.intersection(right_parents)),
                    len(left_tokens.intersection(right_tokens)),
                    -pair_index,
                    (left, right),
                )
            )
            pair_index += 1
    ranked_pairs.sort(reverse=True)
    category_pairs = [
        pair for _, _, _, pair in ranked_pairs[:MAX_CATEGORY_INTERSECTION_PAIRS]
    ]

    cache_key = tuple(category_pairs)
    if intersection_cache is not None and cache_key in intersection_cache:
        intersection_details = intersection_cache[cache_key]
    else:
        intersection_details = commons_api.search_category_intersections(
            category_pairs,
            MAX_CATEGORY_INTERSECTION_RESULTS,
        )
        if intersection_cache is not None:
            intersection_cache[cache_key] = intersection_details
    selected_titles = {f"Category:{category}" for category in selected_categories}
    refinements: list[str] = []
    for title, details in intersection_details.items():
        parents = _dedupe_preserve_order(details.get("parents", []))
        selected_parents = selected_titles.intersection(parents)
        if len(selected_parents) < 2:
            continue
        if int(details.get("file_count", 0)) <= 0:
            continue
        if not _category_title_supported_by_evidence(title, evidence):
            continue
        graph_nodes[title] = {
            "parents": parents,
            "children": [],
            "query_hits": [],
            "source": "intersection",
            "file_count": int(details.get("file_count", 0)),
            "subcategory_count": int(details.get("subcategory_count", 0)),
        }
        for parent in parents:
            parent_node = graph_nodes.get(parent)
            if parent_node is not None:
                parent_node["children"] = _dedupe_preserve_order(
                    parent_node.get("children", []) + [title]
                )
        refinements.append(title.removeprefix("Category:"))

    if not refinements:
        return selected_categories
    print(
        "Refining selected categories with shared child categories: "
        + json.dumps(refinements, ensure_ascii=True),
        flush=True,
    )
    return _remove_ancestor_duplicates(
        _dedupe_preserve_order(selected_categories + refinements),
        graph,
    )
