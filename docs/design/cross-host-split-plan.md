# Plan: 跨 host 拆分 — core / contract / satellite

> **Summary (English).** Design plan for cross-host memory sharing via
> snapshot exchange (no online service — preserving linecite's
> zero-services positioning). Three artifacts assemble through a
> middle layer: **core** (this repo; no network on the sync path),
> **contract** (schemas + MUST clauses + golden vectors, no code; see
> `contract/` and `docs/design/snapshot-contract.md`), and a future
> **satellite** repo owning transport only. Phases: C0 migrates the
> primary key to the host-qualified `chunk_uid` and adds the host
> identity registry; C1/C1.5 implement export/import with a secret
> deny-hook; C2 adds `remote_unverified` freshness semantics and
> host-scoped retrieval with a simulated multi-host goldset; K
> extracts and freezes the contract from the working implementation
> (spec-first design, deliberately late freezing — vectors are
> adjudicated against the prose spec before commitment, and code is
> not the default authority). All phases are implemented in this repo;
> the satellite phase (S) starts only after the protocol stabilizes.



## 決策記錄（2026-09-26/28 討論收斂）

- 情境：跨 host 記憶共享（團隊協作、cross-compile 多機建置）。
- 同步模型：**快照交換**（snapshot exchange），不做線上服務 — 保住
  linecite「zero services, no API key」的核心定位。
- 結構：三個 artifact 組裝，經中間層（contract）解耦：
  1. **core（linecite 本 repo）** — 同步路徑零網路依賴。已知既有例外：
     DuckDB `fts`/`vss` extension 首次 `INSTALL` 可能下載一次
     （store.py:120,618；README 已載明）— 屬索引引擎安裝，與 snapshot
     交換無關，不因本 plan 改變。
  2. **contract（中間層）** — schema + 行為條款（MUST 條款）+ golden
     test vectors。**不含實作**；conformance runner 是消費它的工具，
     v0 放在 core（見 Phase K）。
  3. **satellite（新 repo，暫名 linecite-sync）** — 永不碰 DuckDB 內部，
     只透過 core 公開 API 工作。
- 順序原則：**從能跑的實作提取契約**，不先設計協定。
- **規格內容的權威文件**：`docs/design/snapshot-contract.md`（draft,可自由改；
  Phase K 凍結才是承諾）。分工：draft 管 WHAT（格式/語意/條款）,
  本 plan 管 HOW/WHEN（phase/驗收）。規格改動**先落 draft**,
  本 plan 引用之 — 避免雙文件漂移。
- contract 可以「規範」行為，但實作與合規負擔落在資料所在側
  （驗證需讀本地檔案、檢索需持有 DB）。
- 驗證狀態詞彙表（本文用「狀態 enum」指它）：現有
  `source_missing` / `content_drift` / `no_raw_text` + 本 plan 新增的
  `remote_unverified`。

## 所有權邊界

| 項目 | 歸屬 | 理由 |
|---|---|---|
| snapshot export/import 實作 | core | chunk 序列化是 provenance-keyed upsert 的自然延伸 |
| snapshot/manifest schema、格式版本 | contract | 兩邊實作的共同依據 |
| 狀態 enum（含新增 remote_unverified） | contract 定義、core 實作 | 行為條款可規範，程式碼在 validate_verbatim |
| host 命名/相等性規則 | contract 定義、core 執行 | 身分模型在 core 的 chunk_uid |
| scoped retrieval、跨 host dedup | core | 查詢行為，需持有 DB |
| 錯誤語意（截斷/未知版本/未知狀態 enum 值） | contract | 不明文則兩邊各猜 |
| transport（git/S3/rsync）、交換編排 | satellite | 純傳輸 |
| secret deny-list 政策執行點 | core export hook + satellite 傳輸閘 | 見 Phase C1.5 — 只擋 satellite 太晚，export 已落盤 |
| onboarding 指引 | satellite | 契約定好後 ≈ 10 行 |
| IR 指標（goldset、drift 指標） | core | 量記憶核心 |
| 協定指標（收斂、roundtrip、時效） | satellite | 量同步正確性 |

## Phase C0 — core: 主鍵遷移（前置，新發現的既有缺口）

現況：`chunks.chunk_id` 是 PRIMARY KEY（store.py:167），而 `chunk_id` =
hash(source_path, text)（identity.py），**不含 host** — 兩個 host ingest
同一 repo 的相同內容,import 時必撞主鍵。`chunk_uid`（含 host，
identity.py docstring 明言為 cross-host shared index 設計）已存在但
不是鍵。

1. 遷移主鍵至 `chunk_uid`；`chunk_id` 降為 legacy dedup 欄位保留
   （**非唯一**）— 遷移含**重建 FTS 索引與所有依賴查詢**：
   `create_fts_index` 的 document id 與 `match_bm25(chunk_id, ?)`
   （store.py:638）改以 `chunk_uid` 為準;逐一盤點所有假設
   chunk_id 唯一的檢索/寫入路徑（如 store.py:357 的 legacy dedup）
   再降級
2. 遷移政策定案：**pre-release 不保證 DB 相容,重新 ingest**
   （CHANGELOG 記錄）。因此 `chunk_uid` 可為 **NOT NULL** PK —
   C0 之後的 store 不會產生 NULL uid 列。C2 對 legacy 列
   （host/uid 為 NULL）的語意條款保留為**防禦性行為定義**
   （validate_verbatim 可能收到外部構造或舊物件），非本 store
   會產出的狀態；citation_checks 的 `chunk_uid | None` 同理
3. 新增 `manifests` 表，區分兩種指紋（不可混用）：
   - `host_identity_fingerprint`：host 的**穩定身分**（首次 export 時
     生成並持久化於來源 host 的 store,隨 manifest 出貨）
   - `manifest_digest`：**單次快照**的內容摘要（每次 export 必然不同）
   碰撞規則：同 host id 而 `host_identity_fingerprint` 不同 → 報錯
   （撞名的不同機器）；同 host id + 同 identity、新 `manifest_digest`
   → 正常更新,走對帳
4. store 增加**本機身分**持久化（`local host id` +
   `host_identity_fingerprint`,首次設定後不可變）— C1 self-import
   檢查與 C2 `local_host` 的權威來源
5. **host 身分 registry 對全部寫入路徑生效**（不變量不能只靠
   import 守）：`ingest_paths` / `upsert_snapshot` / `write_chunk`
   檢查 host：
   - host == 本機身分 → 允許
   - host ≠ 本機且已註冊 → **僅允許經 import 內部交易情境**
     （import_snapshot 通過重放檢查後設置的內部 token）— 公開
     API 直寫已註冊遠端 host 一律 reject，否則重放防護可被繞過
   - 未註冊 → reject
   - 模擬/測試逃生口：`register_host(id, fingerprint,
     allow_direct_writes=True)` 顯式開啟該 host 的直寫，文件明載
     此開關**跳過重放防護**,僅供測試與單機模擬（C2 goldset 用它）
6. 驗收：
   - [ ] 兩個 host key、相同 repo 內容,upsert 共存不撞鍵（pytest）
   - [ ] FTS 檢索在遷移後回傳正確 chunk（含兩 host 同內容場景）
   - [ ] 同 host 的第二次快照 import 走更新不報錯；撞名不同機器報錯
   - [ ] 未註冊 host 經任一寫入路徑寫入 → reject（pytest 覆蓋
     三條路徑）
   - [ ] 全測試套件回歸通過

## Phase C1 — core: export/import（單一實作先行）

1. `export_snapshot(store, origin_repo, out_dir, *, meta)`：
   - `chunks.jsonl`：**契約欄位**明文列舉 = chunk_uid、chunk_id、host、
     origin_repo、source_path、source_type、start_line、end_line、text、
     raw_text、content_sha、chunk_ordinal，以及**可空 metadata 欄**
     heading_path、artifact_type、language、quality_tag（store 既有
     且可作 filter 的欄位 — 不入契約會被 matched import 靜默抹掉）。
     **排除**：embedding / embedded_at / embedding_model（「stored
     vectors are never silently re-embedded」的對偶：也不 silently
     跨機搬運 — v0 收方自建索引;三者皆本地儲存 metadata）、
     extension columns（宿主自定義,不進契約 v0）
   - `manifest.json` 必填欄位：host id、`host_identity_fingerprint`
     （來自 C0.4 的本機身分）、`content_digest`、`snapshot_seq`、
     `manifest_digest`、origin_repo、format_version、chunk 數、
     exported_at；以及 **caller 經 `meta` 提供**的 repo URL@SHA、
     ingest globs、ingest 時間 — 這些 store 目前**沒有持久化**
     （已確認），v0 由 caller 供給；持久化 ingest metadata 列為
     C1 後續選項,不阻塞。新 format_version 的 import 缺任一
     必填欄位 → reject
   - **兩個 digest 的分工（不可混用）**：
     `content_digest` = sha256(chunks.jsonl 位元組) — **重放/冪等
     判定的唯一內容鍵**，不含任何易變欄位；
     `manifest_digest` = sha256(chunks.jsonl 位元組 ‖ manifest 去除
     manifest_digest 欄後之正準 JSON) — **純傳輸完整性檢查**
     （蓋含 exported_at 等易變欄位，import 重算不符 → reject 竄改，
     **不參與重放判定**）
   - `snapshot_seq`：由 export 端 store **per (host, origin_repo)
     stream 持久化**；僅當 content_digest 相對該 stream 上次 export
     有變時遞增（deny 配置變動經 chunks.jsonl 內容自然反映）。
     內容未變 → 同 seq 同 content_digest，與 import 冪等 no-op
     規則閉合，確定性驗收不受 exported_at 影響
   - **正準化位元組（可互通定義）**：chunks.jsonl 每列 = UTF-8
     JSON、鍵排序、compact separators、`\n` 結尾；manifest 正準
     JSON = UTF-8、鍵排序、compact、時間戳一律 RFC3339 UTC `Z`。
     import 端按同一定義重算兩個 digest 驗證
   - **export 的 host 身分不由 caller 供給**：一律取 store 持久化
     的本機身分（C0.4），簽名去掉 host 參數；v0 **只允許 export
     本機分區**（chunk.host == 本機 host id），re-export 已 import
     的遠端分區（轉發）明文排除,列 satellite future work
   - 輸出按 chunk_uid 排序 — golden vectors 穩定性前提
2. `import_snapshot(store, dir, *, force=False)`：以 manifest 的
   host key 走 upsert_snapshot 對帳。**matched update 以快照為權威：
   覆寫全部契約欄位**,含四個可空 metadata 欄（heading_path、
   artifact_type、language、quality_tag）— 快照值為 NULL 則寫 NULL,
   不做「缺值保留舊值」的隱性路徑（測試逐欄覆蓋,防 roundtrip
   相等卻靜默保留目標側 metadata）。匯入列的 `embedded_at`（schema
   必填,但 embedding 不隨快照走）由 import 合成 = import 當下時間 —
   屬**本地儲存 metadata,不在契約欄位、不入 roundtrip 斷言**。self-import 檢查以 **C0.4 持久化的本機身分**
   為準（比對 host id + host_identity_fingerprint，不從 chunks 列
   推斷）：snapshot 身分 == 本機身分 → 拒絕。host 碰撞規則
   見 C0.3（identity 不同才報錯,新 manifest_digest 是正常更新）
3. 錯誤語意先在 core 內定案（之後進 contract）：
   - chunks.jsonl 與 manifest 計數/manifest_digest 不符 → reject 整包
   - **逐列安全驗證**（upsert 前,全列通過才進對帳;import 是
     不可信輸入面）：`source_path` 必須為正準 POSIX 相對路徑
     （非絕對、無 `..` 分段）；`chunk_ordinal` 為整數 ≥ 0；
     `start_line` ≥ 1 且 `end_line` ≥ `start_line`（寫入時行契約
     對 import 同樣生效 — 含 **matched-update 路徑重查**：現行
     `upsert_snapshot` 更新行欄位不重驗,C1 補上）；任一不符 →
     reject 整包。測試含 path traversal 列與非法行範圍列,
     以及 matched chunk_uid 更新情境
   - **逐列身分驗證**（upsert 前,全列通過才進對帳）：
     `content_sha == sha256(text)`、`chunk_id == identity.chunk_id(
     source_path, text)`、`chunk_uid == identity.chunk_uid(host,
     origin_repo, source_path, chunk_ordinal, content_sha)` —
     任一不符 → reject 整包（身分是算出來的,不是宣告的）
   - **分區一致性**：任一列的 host / origin_repo ≠ manifest 宣告值
     → reject 整包（在 registry/重放檢查與 upsert **之前**執行 —
     import 內部 token 只授權 manifest 宣告的單一分區,混裝快照
     不得夾帶其他分區的寫入）
   - format_version 高於已知 → reject；同版本多出欄位 → 忽略
   - 未知狀態 enum 值 → reject（enum 是封閉集,版本升級才能擴）
   - **重放/回滾防護**：per (host, origin_repo) 以 `snapshot_seq`
     單調判定（不用 exported_at — 時鐘不可信）：
     seq > 已記錄最後值 → 接受；seq 相等且 **content_digest** 相同 →
     冪等 no-op（exported_at/manifest_digest 不同無妨;仍更新
     manifests 表的 last_seen/imported_at 與最新 exported_at/
     manifest_digest — 純審計,不影響重放序）；seq 相等而
     content_digest 不同、或 seq 較小 → reject。
     `import_snapshot(store, dir, *, force=False)` — `force=True`
     可覆寫,manifests 表記錄被覆寫的前值（seq/digest/exported_at）
     與覆寫時間
4. 驗收：
   - [ ] export→import roundtrip 後**契約欄位**逐欄相等（pytest；
     embedding/embedded_at 明文除外）,含 markdown heading_path
     roundtrip；matched import 不抹掉既有 metadata（專測）
   - [ ] 截斷檔/竄改 manifest/自我 import（依本機身分判定）/
     同名 host 而 identity 指紋不符/逐列身分欄位竄改/
     混入異分區列,六種情境都有測試且 reject
   - [ ] **無部分寫入**：前段列合法、後段列非法的包 reject 後,
     store 與 manifests 表狀態不變（交易性專測）
   - [ ] 確定性：store 無變更下連續兩次 export 的 **chunks.jsonl
     位元組相同、snapshot_seq 相同**；manifest 除 exported_at 外
     逐欄相同（凍結時鐘則含 manifest_digest 全檔位元組相同）
   - [ ] 較小 snapshot_seq 被拒；同 seq 同 content_digest（異
     exported_at）冪等 no-op；同 seq 異 content_digest 被拒；
     force=True 可覆寫且留痕（四情境 pytest）
   - [ ] 對帳刪除語意：快照為 stream 完整權威狀態,缺席列被刪
     （pytest）;chunk_count==0 而無 empty:true → reject
     （mass-delete 防護,契約 §8）

## Phase C1.5 — core: export 端 deny-list hook

secret 只擋在 satellite 傳輸層太晚 — export 已把逐字內容落盤。
1. `export_snapshot(..., deny=patterns)`：命中 deny-list 的 chunk
   不寫出並記入 manifest（`excluded_count`）,預設 deny 集含
   `.env`、常見金鑰路徑模式。`excluded_count` 屬**審計/傳輸
   metadata,不參與重放判定**：deny 變動若未改變寫出的 chunks.jsonl
   → 同 seq 同 content_digest,import 冪等 no-op（有測試覆蓋）
2. satellite 的掃描（Phase S）降為第二道閘：驗 manifest 宣告 +
   內容抽查
3. 驗收：
   - [ ] 種子假金鑰樣本被 export 擋下且 manifest 有記錄（pytest）

## Phase C2 — core: 遠端 freshness + scoped retrieval

1. API 形狀（不只是加一個 reason）：
   - `validate_verbatim(chunk, repo_root, *, local_host=None)`：
     `local_host` 給定且 `chunk.host != local_host` →
     `CitationCheck(False, "remote_unverified")`，不讀檔。
     未給 `local_host` → 現行為不變（回溯相容）
   - `local_host` 的**權威來源**是 store 持久化身分（C0.4），caller
     從那裡取值傳入。host **字串**比對即充分，依據以下不變量
     （成文為 contract 條款）：**store 內 host id ↔
     host_identity_fingerprint 的一對一由 C0.5 的全寫入路徑
     registry 檢查維持**（不只 import）— 撞名不同機器進不了
     同一個 store，故 chunk 層不需攜帶指紋
   - legacy 列（`chunk.host` 為 NULL/缺值,pre-provenance 資料）：
     即使給了 `local_host` 也**維持現行驗證語意**（讀本地檔比對），
     不回 remote_unverified — 與 README 對 legacy 列的
     `no_raw_text` 處理精神一致
   - `Retriever.retrieve(..., verify_root=..., local_host=...)` 傳遞之
   - `RetrievalResult` 新欄位 `citation_checks`：**per-result 記錄
     列表**（與 `chunks` 同序）,每筆 = (chunk_uid | None, chunk_id,
     reason | None) — legacy 列 chunk_uid 可為 NULL,故不能用
     dict[chunk_uid] 當鍵（會把多筆 legacy 失效摺疊到 None）。
     現有 `stale_citations` 以 chunk_id 標識,跨 host 有歧義 —
     保留但標 deprecated
2. scoped retrieval：store 的 `filters=` 已支援 host/origin_repo 欄位
   （非從零實作）— C2 做的是**公開的人體工學包裝** +
   `scope=hosts` 參數 + 測試
3. 跨 host 同 content_sha 的 dedup policy：預設全回、標同組 id，
   不靜默去重 — 去重是 caller 的決策
4. 模擬多 host goldset（不需要 satellite 存在）：
   - 前置：對兩個非本機 host 先 `register_host(...,
     allow_direct_writes=True)`（C0.5 逃生口）,否則 registry
     不變量會擋掉 seeding
   - 同一 DB 塞 3 個 host key，安插近重複文件當 distractor
     （cross-compile 情境：多 toolchain repo 的相似 README）
   - goldset row 增加 `scope` 欄；**scope 由 retrieval runner 消費**
     （查詢時轉成 host filter）— scorer 不動,expected 用
     host-qualified 字串前綴即可比對
5. 驗收：
   - [ ] local_host 未給時全部現有測試不變（回歸）
   - [ ] remote chunk → remote_unverified、本機 chunk 行為不變
   - [ ] scope filter 正確性 pytest
   - [ ] 模擬多 host goldset ≥ 15 條跑通，distractor 類 query 的
     scoped vs unscoped hit@5 有可量測差距

## Phase K — contract 提取（C0–C2 全綠之後）

1. 從真實 export 輸出凍結 schema v0，明標 **unstable**
2. golden test vectors：≥ 4 個真實 export 快照 + 預期 import 結果
   （含 1 個錯誤情境向量 + 1 個逐列身分竄改向量）
2.5 **凍結前防護（防「供奉實作意外」）**：每個 vector 凍結前逐項
   對照 docs/design/snapshot-contract.md 的規格文字驗證 — 分歧一律顯式
   裁決為「code bug」或「spec bug」並修正該側,**不默認 code 為準**；
   裁決記錄附在 vector 旁（一行理由）。draft 未涵蓋而 code 已有的
   行為 → 先補 draft 條款再凍結該 vector
3. 行為條款成文：remote_unverified MUST 條款、錯誤語意表、
   host 相等性規則、狀態 enum 封閉性
4. conformance runner：**放 core**（contract 只有 schema + vectors +
   條款文件，不含程式碼）；core CI 掛上
5. 驗收：
   - [ ] core 通過自己的 conformance run
   - [ ] schema 文件 + vectors 可獨立閱讀（未來可整包搬走）
   - contract 的家 v0 = core 子目錄（避免三 repo 稅），獨立 repo
     等 satellite 成形再議

## Phase S — satellite（協定穩定後才開 repo）

1. transport 編排：拿 core 的 export 目錄經共享通道送達與拉取
   （先支援 git repo / 檔案目錄兩種）
2. 傳輸閘掃描：驗 manifest 的 deny-list 宣告 + 內容抽查
   （第二道閘,第一道在 C1.5）
3. onboarding 指引（此時應 ≈ 10 行：ingest → export → push）
4. 協定指標（satellite 自有,非 IR 指標）：
   - 收斂性：A/B 互換快照後同 scope 查詢結果一致；重複 import 冪等
   - roundtrip 保真：export→transport→import 契約欄位無損
   - **快照時效**：remote chunk 所屬 manifest 的 `exported_at` age
     分佈（v0 以 manifest 粒度計 — per-chunk last-verified 需要 core
     持久化驗證時間戳,目前沒有,列為 future work 不入 v0 指標）
5. 驗收：
   - [ ] 兩個模擬 host 目錄互換後收斂測試通過
   - [ ] 傳輸閘擋下 manifest 未宣告的種子樣本
   - [ ] 指引照做一次成功（自己走一遍計時）

## 不做（明文排除）

- 線上服務/節點發現/auth — 違反定位
- 跨機搬運 embedding — v0 排除（收方自建索引）
- 轉發（re-export 已 import 的遠端分區）— v0 排除,satellite future work
- fuzzy relocation — 等 drift-metrics plan 的 anchor-drift 占比數字
- per-chunk 驗證時間戳持久化 — future work（見 Phase S 指標）
- 即時同步/衝突自動解決 — 快照交換的對帳已覆蓋
- 半衰期 headline 數字 — 依 drift review 結論

## 依賴與風險

- **C0 是新發現的硬依賴**：主鍵 host 無關是既有缺口,不修則 C1 的
  import 在最常見場景（團隊共 repo）必炸 — 本 plan 最大的
  「隱藏工作量」已顯式化
- manifest metadata 依賴 caller 供給（v0）— ingest CLI 順手帶出
  這些值的改動小,但列為 C1 內項目而非假設已存在
- C2 goldset 與 local/goldset-plan.md（local 工作檔,未入庫）共用出題紀律
  （詞彙重疊為報告分佈,不設硬門檻 — 依前次 codex review）
- 主要風險：contract 提取過早。守則：C0–C2 沒有全綠不進 Phase K
- Solo 時程參考：C0 約 2–3 天（含 FTS 重建與唯一性盤點）、C1 約 2–3 天、C1.5 約 0.5 天、
  C2 約 2–3 天、K 約 1–2 天、S 約 3–5 天；各 phase 自帶價值,可中斷
