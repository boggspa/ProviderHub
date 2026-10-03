# AntiGravity fixed-thinking effort fallback

Provider Hub exposes all eight effort positions for every AntiGravity family:
`none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max` and `ultra`.

Since agy 1.2.16 the Claude rows are Opus 5.5 and Sonnet 5.5, each with
`low`, `medium` and `high` native rows. They replaced the single
`claude-opus-4-6-thinking` and `claude-sonnet-4-6` rows, and unlike those they
accept `--effort`. Both Claude families therefore route like the Gemini Flash
families: the slider picks the native row and the matching `--effort` value.

| Provider Hub family | Native rows | Native reasoning control |
| --- | --- | --- |
| `claude-opus-5.5` | `claude-opus-5-5-{low,medium,high}` | `--effort`, default High |
| `claude-sonnet-5.5` | `claude-sonnet-5-5-{low,medium,high}` | `--effort`, default High |
| `gpt-oss-120b` | `gpt-oss-120b-medium` | Medium only |

GPT-OSS accepts `--effort medium`, but this route exposes no alternative
native rung. Provider Hub therefore selects its fixed row without an effort
flag. This applies to both the family ID and the native row alias.

For GPT-OSS, an explicit slider selection becomes a prompt preference with the original
rank preserved, including distinctions such as High, Max and Ultra. It asks
for the corresponding depth and checking while preserving the user's answer
format and length. This is a best-effort instruction, not a native reasoning
budget guarantee. `none` asks for a direct answer; it cannot disable thinking.
Messages requests with only a thinking control also retain that preference.
When neither control is present, no preference is added.

Gemini and Claude families retain their native effort mapping. These changes do not alter
GPT-OSS routes through other providers. GPT-OSS itself supports Low, Medium
and High according to the [OpenAI model documentation](https://developers.openai.com/api/docs/models/gpt-oss-120b);
AntiGravity's available row determines this adapter's native capability.

Regression coverage exercises every slider position, family and native row
selection, catalogue projection, the Messages and Responses protocol paths,
thinking-only requests, defaults and invalid inputs. Existing context and host
tool handling remain covered by the AntiGravity tests.

Live checks on agy 1.2.7 reproduced the old Claude 4.6 rows rejecting
`--effort` before the fixed-row change; Opus, Sonnet and GPT-OSS then each
completed a structured `OK` request with Ultra selected. On agy 1.2.16,
`claude-sonnet-5-5-low --effort low` and `claude-opus-5-5-high --effort high`
each completed an `OK` turn under the route's read-only flags.

The desktop gateway uses bundled worker files. Rebuild Provider Hub and reload
its worker to pick up this source change; a source edit does not update an
already running installed bundle.
