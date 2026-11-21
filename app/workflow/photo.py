"""LangGraph for reading an uploaded or previously stored photo."""

from typing import Callable, NotRequired, TypedDict

from langgraph.graph import END, START, StateGraph

from app.api.database import remote
from app.photo import extractor, vision


class PhotoState(TypedDict):
    mime: str
    document_id: str | None
    data: bytes | None
    storage_path: NotRequired[str]
    result: NotRequired[dict]


def load_original(state: PhotoState) -> dict:
    if state["data"] is not None:
        return {}
    with remote("/storage/v1/object/authenticated/originals/" + state["storage_path"]) as response:
        return {"data": response.read()}


def compile_photo_graph(reader: Callable):
    def recognize(state: PhotoState) -> dict:
        return {"result": reader(state["data"], state["mime"], state["document_id"])}

    graph = StateGraph(PhotoState)
    graph.add_node("load_original", load_original)
    graph.add_node("recognize", recognize)
    graph.add_edge(START, "load_original")
    graph.add_edge("load_original", "recognize")
    graph.add_edge("recognize", END)
    return graph.compile()


stored_photo_graph = compile_photo_graph(extractor.extract_document)
preview_photo_graph = compile_photo_graph(vision.extract_document)
