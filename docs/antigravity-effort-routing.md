# AntiGravity fixed-thinking effort fallback

Provider Hub exposes all eight effort positions for AntiGravity's Claude Opus
4.6, Claude Sonnet 4.6 and GPT-OSS 120B: `none`, `minimal`, `low`, `medium`,
`high`, `xhigh`, `max` and `ultra`.

The installed agy 1.2.7 catalogue provides only these native rows for them:

| Provider Hub family | Native row | Native reasoning mode |
| --- | --- | --- |
| `claude-opus-4.6` | `claude-opus-4-6-thinking` | Thinking, always enabled |
| `claude-sonnet-4.6` | `claude-sonnet-4-6` | Thinking, always enabled |
| `gpt-oss-120b` | `gpt-oss-120b-medium` | Medium |

Both Claude rows reject `--effort`, including `--effort high`, before a model
turn starts. GPT-OSS accepts `--effort medium`, but this route exposes no
alternative native rung. Provider Hub therefore selects each fixed row without
an effort flag. This applies to both family IDs and native row aliases.

An explicit slider selection becomes a prompt preference with the original
rank preserved, including distinctions such as High, Max and Ultra. It asks
for the corresponding depth and checking while preserving the user's answer
format and length. This is a best-effort instruction, not a native reasoning
budget guarantee. `none` asks for a direct answer; it cannot disable thinking.
Messages requests with only a thinking control also retain that preference.
When neither control is present, no preference is added.

Gemini families retain their native effort mapping. These changes do not alter
GPT-OSS routes through other providers. GPT-OSS itself supports Low, Medium
and High according to the [OpenAI model documentation](https://developers.openai.com/api/docs/models/gpt-oss-120b);
AntiGravity's available row determines this adapter's native capability.

Regression coverage exercises every slider position, family and native row
selection, catalogue projection, the Messages and Responses protocol paths,
thinking-only requests, defaults and invalid inputs. Existing context and host
tool handling remain covered by the AntiGravity tests.

Live checks on agy 1.2.7 reproduced both Claude argument rejections before the
fix. After the change, Opus, Sonnet and GPT-OSS each completed a structured
`OK` request with Ultra selected. GPT-OSS briefly returned a provider capacity
error during the earlier probe; that was separate from effort validation.

The desktop gateway uses bundled worker files. Rebuild Provider Hub and reload
its worker to pick up this source change; a source edit does not update an
already running installed bundle.
