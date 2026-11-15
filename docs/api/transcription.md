# 语音转写

编排应用用「语音转写」节点把一段音频（或视频的声音）转成文字，带每句和每个字的时间。教练做视频靠它把配音断成字幕句、按句子排镜头。

落在哪一层：**平台**。转写模型是网关里的一个别名（用途「转写」），节点类型 `transcribe` 是平台的通用节点，调模型经 `aep.llm`，受数据外发策略管。任何应用都能用，应用里只写节点设置。

## 转写模型

网关（`infra/litellm/config.yaml`）里的别名，`model_info.aep_purpose` 写 `transcription`，`mode` 写 `audio_transcription`。模型页把它列在「转写」下面，和对话、向量、重排分开。转写模型只在配置文件里加，页面上不能新增。

对模型的要求：OpenAI 兼容的 `POST /v1/audio/transcriptions`，接受 `response_format: verbose_json` 和 `timestamp_granularities: ["segment", "word"]`，回答里有 `segments`（每句的起止秒数），最好也有 `words`（每个字或词的起止秒数）。OpenAI 的 whisper-1、自己部署的 faster-whisper（[speaches](https://github.com/speaches-ai/speaches)）都行。

仓库带一个别名 `transcribe`，型号、地址和 key 从环境变量读：

| 变量 | 本地编排默认 | 说明 |
| --- | --- | --- |
| `TRANSCRIBE_MODEL` | `openai/Systran/faster-whisper-small` | LiteLLM 的型号写法 |
| `TRANSCRIBE_API_BASE` | `http://asr:8000/v1` | 转写服务的地址 |
| `TRANSCRIBE_API_KEY` | `none` | 自己部署的不用 key |

公司网关没有带时间的转写模型（2026-11-15 实测），所以运行包（`infra/runtime/compose.yml`）和本地编排（`docker-compose.yml`）都带一个可选的本地转写服务 `asr`（speaches，CPU 版），别名默认指向它：

- 打开 `asr` profile 才启动：`docker compose --profile asr up -d`，或在 `.env` 里写 `COMPOSE_PROFILES=asr`（运行包写在 `infra/runtime/.env`，`install.sh` 照常启动）。
- 第一次启动时下载 `ASR_MODEL`（默认 `Systran/faster-whisper-small`，约 500 MB）到 `asr-models` 卷，下完才算健康；以后直接用卷里的。`TRANSCRIBE_MODEL` 默认跟着 `ASR_MODEL`。下载慢时设 `HF_ENDPOINT` 换镜像站。
- 模型一直留在内存里（small 约 1 GB）。内存够时换 `Systran/faster-whisper-medium` 更准；CPU 上 small 大约一分钟的音频转半分钟。
- 本地编排把它映射到 `127.0.0.1:8010`，方便直接试；运行包不对外开放。

测试和 CI 不连转写服务：后端测试的假网关（`tests/conftest.py` 的 `FakeGateway`）回答 `/v1/audio/transcriptions`。

## 节点 `transcribe`

```jsonc
{"id": "asr", "type": "transcribe", "title": "转写配音",
 "model": "transcribe",              // 必填，转写模型的别名
 "audio": "{{start.voiceover}}",     // 必填，要转写的文件，见下
 "language": "zh",                   // 可选，2–3 个小写字母（ISO 639），null 让模型自己判断
 "prompt": "以下是普通话的健身教学。卧推、硬拉、深蹲。"}   // 可选，最多 500 字符，提示用词和标点，可以引用变量
```

`audio` 写恰好一个引用，或者一个媒体路径（相对 `AEP_MEDIA_DIR`）。引用取到的值可以是：

- 素材摘要：开始节点的素材输入、存进媒体库的文件，用它的 `path`。
- 文件引用 `{"aep_file": {"path": …}}`：工具产出、还没存库的文件。
- 媒体路径字符串。

文件要在媒体卷里（库里的文件或 `_work` 下的工作文件），扩展名是音频或视频（同媒体库），不超过 `AEP_TRANSCRIBE_MAX_MB`（默认 25）。视频由转写服务自己取声音。

产出：

| 字段 | 说明 |
| --- | --- |
| `text` | 全文 |
| `language` | 模型认出的语言（模型没说时用设置里的 `language`，都没有时为 null） |
| `duration_seconds` | 音频长度，模型没说时为 null |
| `segments` | 每句 `{"start": 0.0, "end": 2.4, "text": "冬天减脂比夏天难吗"}`，按时间先后，文字去掉首尾空白，空句去掉 |
| `words` | 每个字或词 `{"start": 0.0, "end": 0.3, "word": "冬天"}`；模型不给时为 `[]` |

秒数保留三位小数。无分支，可以设 `on_error`。

### 检查和失败

保存时：

- `model` 没写：「请选择转写模型」；网关上没有这个别名：同调用模型节点。
- `audio` 没写：「请选择要转写的文件」。
- `language` 不对：「语言要写 2–3 个小写字母，比如 zh、en」。
- `prompt` 超长：「提示最多 500 个字符」。

运行时，节点以 `input_error` 失败：

- 取到的值不是文件：「要转写的不是文件：…」。
- 路径在媒体卷外、带 `..`、是软链接：「文件不在媒体库里：…」；文件不存在：「文件不见了：…」。
- 扩展名不是音频或视频：「只能转写音频或视频：…」；太大：「文件超过 25 MB，转写模型一次只收这么大」。
- 外发策略不允许这个模型，或不允许发音频（见下）。

调用失败时 `model_error`「转写失败：…」，超时 `timeout`。

## 数据外发策略：媒体

数据外发策略加一类「媒体」：哪些种类的媒体文件可以发给模型。

- 种类：`audio`（音频，转写节点发的；视频交给转写服务也算音频）和 `image`（图片，看图的节点发的）。
- 平台默认 `AEP_DATA_MEDIA`，逗号分隔，默认 `audio,image`（都允许）；写空表示都不允许。
- 项目的 `policy.media`：`["audio"]`、`[]` 这样的列表，null 表示用平台默认。项目可以比平台严，也可以松，同 `samples`。
- 运行创建时定下，存在运行上，规则同别的两项。以前的运行没有这一项，按都允许处理（那时还没有节点发媒体）。
- 不允许时，节点在发出请求前以 `input_error` 失败：「平台不允许把音频发给模型，节点「转写配音」要发」；项目设的写「项目「x」不允许……」。
- 模型白名单（`models`）照旧管转写模型和看图模型。

`GET /projects/options` 加 `media_kinds: [{"value": "audio", "label": "音频"}, {"value": "image", "label": "图片"}]`，`defaults` 加 `media`。项目页的外发策略里加「可以发给模型的媒体」：跟随平台默认，或自己勾选音频、图片。

## 调用记录

转写调用记一行 `model_calls`：`kind` 为 `transcription`，`prompt_tokens`、`completion_tokens` 为 0，新加的 `audio_seconds` 记这段音频的秒数（模型没说时为空）。模型页的使用情况加「音频分钟」，只有转写模型有。

模型页测试转写模型时发一段 1 秒的静音 WAV，有回答就算通过。

## 测试

- 节点：设置的检查；引用素材摘要、文件引用、媒体路径都能转写；产出的整理（时间保留三位小数、空句去掉、没有 `words` 时为 `[]`）；路径在卷外、软链接、不存在、扩展名不对、太大时 `input_error`；模型报错时 `model_error`，`on_error` 分支照常。
- 外发策略：平台默认、项目设置、运行上存的策略；不允许音频时不发请求；以前的运行按都允许。
- 调用记录的 `kind` 和 `audio_seconds`；模型页按用途分组、测试转写模型。
