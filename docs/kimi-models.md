# Kimi Code model catalogue

Provider Hub refreshes Kimi from its bundled documentation-based catalogue.
It does not query Kimi CLI's account model list, so a new upstream model needs
a catalogue update in Provider Hub before Refresh can list it.

The [official model configuration guide](https://www.kimi.com/code/docs/en/kimi-code/models.html)
documents six IDs, including `k3.1-pro` and `k3.1-pro-highspeed`. Both support
tools, images and low/high/max thinking effort, with high as the default.
Thinking cannot be disabled on either new model; Hub rejects an explicit off
request and enables thinking by default. The older K2.7 Highspeed route still
has its separate fixed-thinking behaviour.

K3.1 Pro's context depends on membership: Plus / Moderato gets 262,144 tokens;
Pro / Allegretto and above gets 1,048,576. Hub preserves that range rather than
claiming every key has the larger tier. K3.1 Pro Highspeed requires Pro /
Allegretto or above and supports 1,048,576 tokens. It is a separate model ID,
uses about twice the quota of regular K3.1 Pro, and has no same-model Fast
toggle. These are documented capabilities; actual API access follows the
account that owns the saved Kimi Code key.

After updating Hub, refresh Kimi's catalogue and enable the new routes for
the desired desktop or Chat picker. Start a new chat for the new model family:
Kimi recommends doing so to preserve output quality and cache efficiency.
Existing saved selections keep their model IDs; Hub never silently changes an
older K3 route to K3.1 Pro.

The local build also fixes a tool-history projection issue: parallel tool
calls and their results remain grouped into complete assistant/user turns.
Previously, a valid batch could be split into separate messages, which strict
upstream adapters rejected as an unanswered `tool_call_id`. The fix preserves
actual results, error flags and reasoning blocks; it does not replay actions
or invent missing results.
