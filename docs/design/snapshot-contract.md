# linecite snapshot contract — DRAFT v0

> **Summary (English).** Interoperability contract for linecite's
> cross-host snapshot exchange. A snapshot package is a directory of
> two files: `chunks.jsonl` (canonical JSON rows, chunk_uid-sorted,
> exactly the §2.1 field set, verbatim `raw_text` required) and
> `manifest.json`. Two digests with strictly separated roles:
> `content_digest` (sha256 of chunks bytes — the only replay/
> idempotence key) and `manifest_digest` (transport integrity, covers
> volatile fields, never used for replay). Identity is computed, not
> declared: importers recompute `content_sha`/`chunk_id`/`chunk_uid`
> per the §4 formulas. Import runs a six-step pipeline in normative
> order (§8) — package integrity, row safety, row identity, partition
> consistency, self-import/host-collision, monotonic-seq replay — and
> rejects whole packages atomically; an accepted snapshot is the full
> authoritative state of its (host, origin_repo) stream, deletions
> included, with an explicit `empty:true` guard against accidental
> mass-delete. Hosts carry a stable random fingerprint with a 1:1
> host-id↔fingerprint invariant enforced on every write path. Frozen
> artifacts (schemas, MUST clauses, golden vectors) live in
> `contract/`; the prose below is authoritative.



**狀態：draft。** 本文件可自由修改;Phase K 凍結（schema 檔 + golden
vectors + conformance）才構成承諾。規格性規則以 **MUST** 標示,
conformance test 逐條對應。文內 wire name 一律用 `code` 格式。
分工：本文件管 WHAT,`split-plan` 管 HOW/WHEN。

**現況差距（informative）**：本契約描述 conforming 實作的**目標
行為**;下列條款現行 code 尚未滿足,由 split-plan 各 phase 落實 —
matched-update 全契約欄位覆寫（現行 `upsert_snapshot` 只更新部分
欄位且表鍵仍為 `chunk_id`,C0/C1 修）、`validate_verbatim` 的
`local_host` 參數與 `remote_unverified`（C2）、host registry（C0）。

## 1. 參與方與邊界

- **producer / importer（core, linecite）**：實作 export/import 與
  查詢語意;同步路徑零網路依賴。
- **transport（satellite）**：搬運快照包,不碰 DuckDB 內部。
  deny-list 抽查屬 transport 自身政策,**非本契約條款**（informative）。
- 本契約不含實作;conformance runner 為消費工具,v0 住 core。

## 2. 快照包

一個目錄,含 `chunks.jsonl` 與 `manifest.json` 兩個檔案。

### 2.1 chunks.jsonl 列 schema（v0）

每列一個 JSON object。**MUST**：UTF-8;每列一個 `\n` 結尾,無空列;
**每列位元組即其正準 JSON 序列化**（§3 正準規則:鍵排序、compact
separators — importer MUST 重序列化比對,不符 → reject;golden
vectors 的位元組穩定性依賴此條）;物件鍵重複 → reject;非有限數值
（NaN/Inf）→ reject;鍵集合 MUST 恰為本節欄位表（多鍵/缺鍵 →
reject）;列序按 `chunk_uid` 位元組升冪;`chunk_uid` 與
(`source_path`, `chunk_ordinal`) 在檔內皆 MUST 唯一,重複 → reject。

| key | 型別 | 約束 |
|---|---|---|
| `chunk_uid` | string | 32 lowercase hex;MUST == §4 重算值 |
| `chunk_id` | string | 16 lowercase hex;MUST == §4 重算值 |
| `host` | string | MUST == manifest `host` |
| `origin_repo` | string | MUST == manifest `origin_repo`;非空 |
| `source_path` | string | §5 路徑文法 |
| `source_type` | string | 非空 |
| `start_line` | int | ≥ 1 |
| `end_line` | int | ≥ `start_line` |
| `text` | string | 檢索用正規化文本;身分計算的輸入（§4） |
| `raw_text` | string | **非 null**（v0;見 §2.3）;引用驗證的權威 |
| `content_sha` | string | 64 lowercase hex;MUST == §4 重算值 |
| `chunk_ordinal` | int | ≥ 0 |
| `heading_path` | string \| null | |
| `artifact_type` | string \| null | |
| `language` | string \| null | |
| `quality_tag` | string \| null | |

任一列違反任一約束 → **reject 整包**。

### 2.2 manifest.json schema（v0）

**MUST**：UTF-8 JSON object;鍵重複 → reject。必填鍵：

| key | 型別 | 語意 |
|---|---|---|
| `format_version` | int | v0 = `0`。高於已知 → reject;`0` 為最低版,無更低相容問題;狀態 enum 擴充 MUST 伴隨版本遞增 |
| `host` | string | 文法：`[A-Za-z0-9][A-Za-z0-9._-]{0,63}` |
| `host_identity_fingerprint` | string | 32 lowercase hex;生成規則見 §6 |
| `origin_repo` | string | 非空 |
| `content_digest` | string | 64 lowercase hex;§3 |
| `snapshot_seq` | int | ≥ 0;§7 |
| `manifest_digest` | string | 64 lowercase hex;§3 |
| `chunk_count` | int | ≥ 0;MUST == chunks.jsonl 列數 |
| `exported_at` | string | 恰為 `YYYY-MM-DDTHH:MM:SSZ`（RFC3339 UTC,無小數秒）;審計用 |
| `excluded_count` | int | ≥ 0;被 export 端 deny-list 排除的 **chunk 數**;僅語法驗證,不參與重放 |
| `empty` | bool | 見 §8 空快照規則 |
| `meta` | object | 必填鍵：`repo_url`、`repo_sha`、`ingest_globs`（string array）、`ingested_at` — 皆可為 null（v0 由 caller 供給） |

新 `format_version` 缺任一必填鍵 → reject。同版本**多出的未知鍵**：
語意上忽略,但 **MUST 納入 manifest_digest 覆蓋**（見 §3）— 否則兩個
conforming importer 會算出不同 digest。

### 2.3 `text` 與 `raw_text`

- `raw_text` = export 當下 `source_path` 第 `start_line`..`end_line`
  行的逐字 `"\n".join`;引用驗證（validate_verbatim）以它為權威。
- `text` = 檢索用正規化文本,MAY ≠ `raw_text`;**身分欄位以 `text`
  計算**（§4,與 identity.py 一致）。
- v0 快照中 `raw_text` MUST 非 null:legacy（無 raw_text）列不可
  export（C0 已定 pre-release 重新 ingest,此類列不存在於
  conforming store）。

## 3. 正準化與 digest

- `content_digest` = sha256(chunks.jsonl 檔案位元組) 的 lowercase
  hex — **重放/冪等判定唯一內容鍵**。sha256 碰撞處理明文
  out of scope。
- **正準 JSON**（manifest 與 chunks.jsonl 每列共用同一規則）：
  UTF-8、鍵按 Unicode code point 排序、compact separators
  （`,` `:` 無空白）、無尾隨換行（列的 `\n` 是行框架,不屬序列化）。
- `manifest_digest` = sha256(chunks.jsonl 位元組 ‖ manifest 去除
  `manifest_digest` 鍵後**所有其餘鍵**（含未知鍵）之正準 JSON)。
  純傳輸完整性;importer MUST 重算,不符 → reject;**不參與重放判定**。
- importer MUST 原樣比對 `exported_at` 字串參與 digest,不得先
  正規化時間戳。

## 4. 身分公式（位元組級,與 identity.py 一致）

編碼一律 UTF-8,**無 Unicode 正規化**;輸出一律 lowercase hex。

```
content_sha = sha256(text.encode("utf-8")).hexdigest()            # 64 hex
chunk_id    = sha256((source_path + "\n" + text).encode()).hexdigest()[:16]
chunk_uid   = sha256(f"{host}\0{origin_repo}\0{source_path}\0"
                     f"{chunk_ordinal}\0{content_sha}".encode()).hexdigest()[:32]
```

**身分是算出來的,不是宣告的**：importer MUST 逐列重算三者,
不符 → reject 整包。

## 5. source_path 文法

MUST 全部成立,否則 reject 整包：非空;不以 `/` 開頭;以單一 `/`
分段;每段非空、非 `.`、非 `..`;不含 `\`、NUL;無尾隨 `/`。
比對為位元組比對,**不做** Unicode 正規化、percent-decoding、
symlink 解析、大小寫摺疊。

## 6. host 身分

- `host` 文法見 §2.2。`host_identity_fingerprint` = 首次需要時
  隨機生成之 128-bit,表示為 32 lowercase hex,與 host id 一同
  持久化於 store（首次設定後不可變）— 此為**本機身分**。
- **不變量（MUST）**：一個 store 內 `host` ↔
  `host_identity_fingerprint` 一對一,由**全部寫入路徑**的 registry
  檢查維持。registry 可觀測狀態：每個已知 host 的
  (id, fingerprint, 來源=local|import, 首見時間)。
- 因此 chunk 層不攜帶指紋,host 字串比對即充分。

## 7. 重放與稽核狀態

importer MUST 對每個 stream（= (`host`, `origin_repo`)）持久化：
`last_seq`、`last_content_digest`、`last_exported_at`、
`last_manifest_digest`、`last_imported_at`;force 覆寫另記
`forced_prev` = 被覆寫的 (seq, content_digest, exported_at) +
覆寫時間。以上更新 MUST 與 chunk 寫入**同一交易**。

producer 端：`snapshot_seq` per stream 持久化;僅當 `content_digest`
相對該 stream 上次 export 有變時遞增。

## 8. import 驗證管線（順序即規範）

1. **包完整性**：manifest schema、`chunk_count`、`manifest_digest`
   重算。
2. **逐列安全**：§5 路徑、`chunk_ordinal`/行範圍約束（行契約對
   matched-update 同樣生效）。
3. **逐列身分**：§4 重算相符。
4. **分區一致**：每列 `host`/`origin_repo` == manifest 宣告值。
5. **身分檢查**（順序固定,錯誤碼相異）：
   a. manifest `host` == 本機 host id 且 fingerprint 相同 →
      reject `self_import`;
   b. manifest `host` == 本機 host id 而 fingerprint 不同 →
      reject `host_collision`;
   c. manifest `host` 已在 registry 而 fingerprint 不同 →
      reject `host_collision`。
6. **重放判定**（per stream）：
   - `snapshot_seq` > `last_seq` → 接受
   - seq 相等且 `content_digest` 相同 → **冪等 no-op**（仍 MUST
     更新 §7 稽核欄位）
   - seq 相等而 digest 不同,或 seq 較小 → reject
     （`force=True` 覆寫,MUST 依 §7 留痕）

**對帳語意（接受時）**：快照為該 stream 的**完整權威狀態** —
匯入 = 對該分區做 insert/update/delete 對帳;store 中屬該 stream
但不在快照內的列 MUST 刪除。**matched update 覆寫全部契約欄位**
（§2.1 全表,快照 null 則寫 null,無「缺值保留」路徑）。

**空快照防護**：`chunk_count == 0` 的包 MUST 帶 `empty: true`,
否則 reject — 呼應 core 的「failed scan can never mass-delete」:
清空分區必須是顯式意圖,不能是損壞快照的副作用。

**原子性（MUST）**：任一步 reject 後,chunks 表、索引、registry、
§7 稽核/序列狀態皆無可觀測變化（單一交易,全有或全無）。

## 9. 行為條款（MUST）

- **remote_unverified**（target,C2 落實）：驗證時給定 `local_host`
  且 `chunk.host != local_host` → 回 `remote_unverified`,不讀檔;
  未給 `local_host` → 單機語意不變。
- **legacy 列**（host/uid 為 null;防禦性定義,conforming store 不
  產出）：給定 `local_host` 仍走本地驗證語意。
- **狀態 enum**（封閉集）：`source_missing` / `content_drift` /
  `no_raw_text` / `remote_unverified`。未知值 → reject;擴充 MUST
  伴隨 `format_version` 遞增。
- **非本機寫入**：涵蓋 API = `ingest_paths`、`upsert_snapshot`、
  `write_chunk`（及未來一切公開寫入面）。已註冊遠端 host 的直寫
  MUST reject（可測錯誤條件）;僅 `import_snapshot` 通過管線
  1–6 後的內部交易情境可寫遠端分區。`register_host(...,
  allow_direct_writes=True)` 為測試/模擬逃生口,跳過重放防護。

## 10. v0 明文排除

跨機 `embedding` 搬運、轉發（re-export 遠端分區）、線上服務/
discovery/auth、per-chunk 驗證時間戳、即時同步/自動衝突解決。

## 11. 凍結程序（Phase K）

golden vectors 從真實 export 凍結;每個 vector 凍結前逐項對照本
文件驗證,分歧顯式裁決為 code bug 或 spec bug 並修正該側,不默認
code 為準,裁決一行理由附於 vector 旁。本文件未涵蓋而 code 已有的
行為 → 先補條款再凍結。
