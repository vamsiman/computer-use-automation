"""Discovery: the one run with a model in the loop."""

from cua.discovery.agent import (
    AnthropicClient,
    DEFAULT_MODEL,
    DiscoveryAgent,
    DiscoveryLimits,
    ModelClient,
    ModelReply,
    ToolCall,
    discover,
    model_name,
)
from cua.discovery.annotate import annotate, apply as apply_annotation
from cua.discovery.distill import NothingToDistil, distil, prune, slug_from_goal
from cua.discovery.locate import synthesize, verifies
from cua.discovery.tools import TERMINAL_TOOLS, TOOLS, UnusableTarget, action_from_tool_call
from cua.discovery.trace import DiscoveryTrace, TraceRecord, tree_hash

__all__ = [
    "AnthropicClient",
    "DEFAULT_MODEL",
    "DiscoveryAgent",
    "DiscoveryLimits",
    "DiscoveryTrace",
    "ModelClient",
    "ModelReply",
    "TERMINAL_TOOLS",
    "TOOLS",
    "ToolCall",
    "TraceRecord",
    "NothingToDistil",
    "UnusableTarget",
    "action_from_tool_call",
    "annotate",
    "apply_annotation",
    "distil",
    "prune",
    "slug_from_goal",
    "discover",
    "model_name",
    "synthesize",
    "tree_hash",
    "verifies",
]
