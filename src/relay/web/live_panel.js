// live_panel.js — v0.130 实时流侧栏脚本（统一容器 + 动态多列 + 自动延展）
//
// Python 后台线程经 evaluate_js 调用 window.relayLiveEvent(ev) 推送事件。
// 事件类型（来自 proxy._broadcast_live_event / live_stream snapshot）：
//
//   snapshot —— { type, request_id, started_at, platform, model,
//                 client_model, upstream, phase, bytes_received,
//                 content_length, user_text_preview, assistant_text,
//                 age_sec, api_key, inbound_wire, outbound_wire,
//                 usage_live, thinking_text }
//                 字段除 api_key 已掩码外，其余来自 _InFlight dataclass。
//                 首连由 /live/stream SSE 推一条。**无 request_id → 只更
//                 endpoint**（不建容器）。
//   delta    —— { type, request_id, assistant_text, thinking_text,
//                 usage_live }（assistant_text/thinking_text 是"到目前为止
//                 的累积文"，不是单次增量）。
//   done     —— { type, request_id, phase="done", assistant_text,
//                 thinking_text, tool_use_json, usage_live, error }。
//
// 入口统一为 relayLiveEvent(ev)，按 request_id 路由：
//   * 首个事件（thinking_text 非空 → thinkstream，否则 puretext）动态建
//     该 rid 的容器，**中途不换类**。
//   * 并发请求各建各的容器，按到达序纵排 / 多列。
//   * endpoint（静态 col1 顶）显示**最新活跃 rid** 的 5 字段。
//   * 工具调用全局拼接在最后一个容器后面（cap 最近 30 条 + 20vh 滚动）。
// 布局由 layout() 显式算高 + 逐容器写 style.height（精确填满 + ±10% 钳制），
// 列数变化时经 bridge 上报 Python（live_panel_layout）决定窗口宽度。

(function () {
  "use strict";

  // ---- DOM 缓存 ----
  const $panelClose = document.getElementById("panel-btn-close");
  const $upstream = document.getElementById("lp-upstream");
  const $badge = document.getElementById("lp-badge");
  const $key = document.getElementById("lp-key");
  const $epList = document.getElementById("lp-ep-list");
  const $endpoint = document.querySelector(".live-panel-endpoint");
  const $toolsWrap = document.getElementById("lp-tools-wrap");
  const $tools = document.getElementById("lp-tools");
  const $toolsCount = document.getElementById("lp-tools-count");
  const $main = document.querySelector(".live-panel-main");

  function getCols() {
    return Array.prototype.slice.call(document.querySelectorAll(".lp-col"));
  }

  // ---- v0.113r：独立小窗 i18n（不依赖主窗 app.js） ----
  // v0.151：5 语言 —— 与 app.js 同步 zh / en / ja / ko / zh-TW
  let LP_LANG = "zh";
  function _readLpLang() {
    try {
      const v = localStorage.getItem("lang");
      return (v === "en" || v === "ja" || v === "ko" || v === "zh-TW") ? v : "zh";
    } catch (_) { return "zh"; }
  }
  LP_LANG = _readLpLang();
  const LP_I18N = {
    en: {
      "实时流": "Live stream",
      "关闭": "Close",
      "空闲": "Idle",
      "上传中": "Uploading",
      "调用上游": "Calling upstream",
      "调用上游…": "Calling upstream…",
      "接收客户端请求…": "Receiving client request…",
      "流式中": "Streaming",
      "流式中…": "Streaming…",
      "出错": "Error",
      "完成": "Done",
      "连接中断": "Connection dropped",
      "入向 → 出向": "in → out",
      "（明文待加载）": "(plaintext loading)",
      "（读取失败）": "(read failed)",
      "（无）": "(none)",
      "输入": "Input",
      "输出": "Output",
      "缓存读取": "Cache read",
      "缓存创建": "Cache write",
      "命中率": "Hit rate",
      "思考": "Thinking",
      "（暂无）": "(none)",
      "流": "Stream",
      "正文流": "Stream",
      "正文": "Stream",
      "工具调用": "Tool calls",
      "暂无并发请求": "No concurrent requests",
      "字": "chars",
      "秒前": "s ago",
      "刚刚": "just now",
      "连接中断（无结束符）": "Connection dropped",
    },
    "zh-TW": {
      "实时流": "即時串流",
      "关闭": "關閉",
      "空闲": "閒置",
      "上传中": "上傳中",
      "调用上游": "呼叫上游",
      "调用上游…": "呼叫上游…",
      "接收客户端请求…": "接收客戶端請求…",
      "流式中": "串流中",
      "流式中…": "串流中…",
      "出错": "出錯",
      "完成": "完成",
      "连接中断": "連線中斷",
      "入向 → 出向": "入向 → 出向",
      "（明文待加载）": "（明文待載入）",
      "（读取失败）": "（讀取失敗）",
      "（无）": "（無）",
      "输入": "輸入",
      "输出": "輸出",
      "缓存读取": "快取讀取",
      "缓存创建": "快取寫入",
      "命中率": "命中率",
      "思考": "思考",
      "（暂无）": "（暫無）",
      "流": "串流",
      "正文流": "串流",
      "正文": "串流",
      "工具调用": "工具呼叫",
      "暂无并发请求": "暫無並發請求",
      "字": "字",
      "秒前": "秒前",
      "刚刚": "剛剛",
      "连接中断（无结束符）": "連線中斷",
    },
    ja: {
      "实时流": "ライブストリーム",
      "关闭": "閉じる",
      "空闲": "アイドル",
      "上传中": "アップロード中",
      "调用上游": "アップストリーム呼び出し",
      "调用上游…": "アップストリーム呼び出し…",
      "接收客户端请求…": "クライアントリクエスト受信…",
      "流式中": "ストリーミング中",
      "流式中…": "ストリーミング中…",
      "出错": "エラー",
      "完成": "完了",
      "连接中断": "接続が切断されました",
      "入向 → 出向": "受信 → 送信",
      "（明文待加载）": "（平文ロード中）",
      "（读取失败）": "（読み込み失敗）",
      "（无）": "（なし）",
      "输入": "入力",
      "输出": "出力",
      "缓存读取": "キャッシュ読み取り",
      "缓存创建": "キャッシュ書き込み",
      "命中率": "ヒット率",
      "思考": "思考",
      "（暂无）": "（なし）",
      "流": "ストリーム",
      "正文流": "ストリーム",
      "正文": "ストリーム",
      "工具调用": "ツール呼び出し",
      "暂无并发请求": "同時リクエストなし",
      "字": "文字",
      "秒前": "秒前",
      "刚刚": "たった今",
      "连接中断（无结束符）": "接続切断",
    },
    ko: {
      "实时流": "실시간 스트림",
      "关闭": "닫기",
      "空闲": "대기 중",
      "上传中": "업로드 중",
      "调用上游": "업스트림 호출",
      "调用上游…": "업스트림 호출…",
      "接收客户端请求…": "클라이언트 요청 수신…",
      "流式中": "스트리밍 중",
      "流式中…": "스트리밍 중…",
      "出错": "오류",
      "完成": "완료",
      "连接中断": "연결 끊김",
      "入向 → 出向": "수신 → 송신",
      "（明文待加载）": "（평문 로딩 중）",
      "（读取失败）": "（읽기 실패）",
      "（无）": "（없음）",
      "输入": "입력",
      "输出": "출력",
      "缓存读取": "캐시 읽기",
      "缓存创建": "캐시 쓰기",
      "命中率": "적중률",
      "思考": "사고",
      "（暂无）": "（없음）",
      "流": "스트림",
      "正文流": "스트림",
      "正文": "스트림",
      "工具调用": "도구 호출",
      "暂无并发请求": "동시 요청 없음",
      "字": "글자",
      "秒前": "초 전",
      "刚刚": "방금",
      "连接中断（无结束符）": "연결 끊김",
    },
  };
  function _lpCurDict() { return LP_I18N[LP_LANG] || LP_I18N.ja; }
  function tl(key) { return (LP_LANG !== "zh" && _lpCurDict()[key]) || key; }
  window.__lpT = tl;
  function applyLivePanelLang() {
    LP_LANG = _readLpLang();
    // v0.151：BCP-47 + body class 多语言
    const htmlLang = LP_LANG === "zh" ? "zh-CN"
      : LP_LANG === "zh-TW" ? "zh-TW"
      : LP_LANG;
    document.documentElement.lang = htmlLang;
    document.body.classList.remove("lang-en", "lang-ja", "lang-ko", "lang-zh-tw");
    if (LP_LANG !== "zh") {
      document.body.classList.add(`lang-${LP_LANG.toLowerCase()}`);
    }
    document.title = tl(document.title);
    const close = document.getElementById("panel-btn-close");
    if (close) { close.title = tl("关闭"); close.setAttribute("aria-label", tl("关闭")); }
    // 静态文本节点（保留 speed/count 子 span）。动态容器（.lp-rid）的
    // 标签也是文本节点，一并在切换语言时重刷。
    const SEL = [
      ".live-panel-token-label", ".live-panel-label", ".live-panel-summary",
      ".live-panel-cache-rate", ".live-panel-key", "#lp-badge",
      "#lp-thinking", "#lp-upstream",
      ".live-panel-thinking-title", ".lp-ep-meta", ".lp-ep-wire",
    ].join(",");
    document.querySelectorAll(SEL).forEach((el) => {
      el.normalize();
      el.childNodes.forEach((n) => {
        if (n.nodeType !== 3) return;
        const k = (n.nodeValue || "").replace(/\s+/g, " ").trim();
        if (!k) return;
        const v = tl(k);
        if (v !== k && n.nodeValue !== v) n.nodeValue = v;
      });
    });
    // 重新应用已知动态状态（endpoint badge / 各 rid 容器 badge 文案）
    if (_badgePhase) setPhaseBadge(_badgePhase, _badgeError);
    Object.keys(_ridEls).forEach((rid) => {
      const rec = _ridEls[rid];
      if (rec && rec.badgePhase) setRidBadge(rec, rec.badgePhase, rec.badgeError);
    });
  }
  window.addEventListener("storage", () => { applyLivePanelLang(); });
  // v0.132：phase → {label, attr} 公共映射，供端点顶部徽标 / 并发列表行 /
  // per-rid 容器徽标三处共用。
  function phaseLabel(phase, errorText) {
    if (phase === "uploading") return { label: "上传中", attr: "uploading" };
    if (phase === "calling") return { label: "调用上游", attr: "calling" };
    if (phase === "streaming") return { label: "流式中", attr: "streaming" };
    if (phase === "done") return { label: errorText ? "出错" : "完成", attr: errorText ? "error" : "done" };
    return { label: "空闲", attr: "idle" };
  }
  let _badgePhase = null;
  let _badgeError = "";
  function setPhaseBadge(phase, errorText) {
    _badgePhase = phase;
    _badgeError = errorText || "";
    const pl = phaseLabel(phase, errorText);
    setText($badge, tl(pl.label));
    $badge.dataset.phase = pl.attr;
  }

  // ---- 粘底判定（按元素 id 记状态） ----
  const _stickyState = {};
  function isSticky(el) {
    if (!el || !el.id) return false;
    const slack = 24;
    const now = Date.now();
    const st = _stickyState[el.id] || (_stickyState[el.id] = { last: true, at: 0 });
    const cur = (
      el.scrollTop + el.clientHeight >=
      el.scrollHeight - slack
    );
    if (cur) {
      st.last = true;
      st.at = now;
      return true;
    }
    if (st.last && (now - st.at) < 500) return true;
    st.last = false;
    st.at = now;
    return false;
  }

  function setText($el, text) {
    if (!$el) return;
    if ($el.textContent !== text) $el.textContent = text;
  }

  function fmtNum(n) {
    if (typeof n !== "number" || !isFinite(n)) return "0";
    if (n >= 100000) return (n / 1000).toFixed(1) + "k";
    return String(n);
  }

  function setTokenValue($el, n, prev) {
    const val = typeof n === "number" && isFinite(n) ? n : 0;
    setText($el, fmtNum(val));
    if (prev !== null && prev !== undefined && val > prev) {
      $el.classList.add("bump");
      setTimeout(() => $el.classList.remove("bump"), 220);
    }
  }

  // ---- v0.97 流式速度（每 rid 容器各自一实例） ----
  function makeRate() {
    let last = null;
    let samples = [];
    let lastRate = null;
    return {
      push(len, now) {
        const prev = last;
        last = { t: now, len };
        if (!prev) return null;
        const dt = now - prev.t;
        const dl = len - prev.len;
        if (dl < 0) {
          samples = [];
          lastRate = null;
          return null;
        }
        if (dl === 0 || dt <= 0) return lastRate;
        samples.push({ t: now, dl, dt });
        while (samples.length && now - samples[0].t > 2000) samples.shift();
        let sd = 0;
        let st = 0;
        for (const s of samples) { sd += s.dl; st += s.dt; }
        if (st <= 0) return lastRate;
        lastRate = (sd / st) * 1000;
        return lastRate;
      },
      reset() {
        last = null;
        samples = [];
        lastRate = null;
      },
    };
  }

  function fmtRate(r) {
    if (r >= 1000) return Math.round(r);
    if (r >= 100) return r.toFixed(0);
    return r.toFixed(1);
  }
  function applySpeed($el, rate) {
    if (!$el) return;
    const ok = rate != null && isFinite(rate) && rate >= 0.5;
    if (ok) {
      $el.textContent = `${fmtRate(rate)} t/s`;
      $el.hidden = false;
    } else {
      $el.textContent = "";
      $el.hidden = true;
    }
  }

  // =====================================================================
  // endpoint（静态 col1 顶）：顶部最新活跃 + 中间并发请求列表 + 底部 api-key
  // =====================================================================
  // _epRids: rid -> { upstream, platform, client_model, model, phase, error, inbound, outbound }
  // 到达序由 Map 迭代序保证（set 追加 / 不重排）。顶部"最新活跃"= _lastEpRid。
  let _epRids = new Map();
  let _lastEpRid = null;
  let _snapTop = null;   // snapshot 事件（无 rid）的字段，作顶部兜底

  function epRecord(ev) {
    let phase = ev.phase || "";
    if (!phase) {
      if (ev.type === "done") phase = "done";
      else if (ev.type === "delta") phase = "streaming";
    }
    return {
      upstream: ev.upstream || "",
      platform: ev.platform || "",
      client_model: ev.client_model || "",
      model: ev.model || "",
      phase,
      error: ev.error || "",
      inbound: ev.inbound_wire || 0,
      outbound: ev.outbound_wire || 0,
    };
  }

  function renderEpList() {
    if (!$epList) return;
    while ($epList.firstChild) $epList.removeChild($epList.firstChild);
    // v0.132b：反向迭代 —— 最新到达的并发请求排在列表最上方（Map 插入序，最后 set 的=最新）。
    const entries = Array.from(_epRids.entries()).reverse();
    entries.forEach(([rid, d]) => {
      const item = document.createElement("div");
      item.className = "lp-ep-item";
      const top = document.createElement("div");
      top.className = "lp-ep-item-top";
      const meta = document.createElement("span");
      meta.className = "lp-ep-meta";
      setText(meta, [d.platform || "—", modelLabel(d) || "—"].filter(Boolean).join(" · "));
      const b = document.createElement("span");
      b.className = "live-panel-badge lp-ep-badge";
      const pl = phaseLabel(d.phase, d.error);
      setText(b, tl(pl.label));
      b.dataset.phase = pl.attr;
      top.appendChild(meta);
      top.appendChild(b);
      const sub = document.createElement("div");
      sub.className = "lp-ep-item-sub";
      const lab = document.createElement("span");
      lab.className = "live-panel-label";
      setText(lab, tl("入向 → 出向"));
      const wire = document.createElement("span");
      wire.className = "lp-ep-wire";
      setText(wire, `${d.inbound || "?"} → ${d.outbound || "?"}`);
      sub.appendChild(lab);
      sub.appendChild(wire);
      item.appendChild(top);
      item.appendChild(sub);
      $epList.appendChild(item);
    });
  }

  function renderEpTop() {
    let d = (_lastEpRid && _epRids.has(_lastEpRid)) ? _epRids.get(_lastEpRid) : _snapTop;
    setText($upstream, (d && d.upstream) || "—");
    const pl = d ? phaseLabel(d.phase, d.error) : { label: "空闲", attr: "idle" };
    setText($badge, tl(pl.label));
    $badge.dataset.phase = pl.attr;
  }

  function applyEndpoint(ev) {
    if (ev.request_id) {
      // v0.132b：delete 再 set → 把该 rid 移到 Map 末尾（插入序=最新活动序），
      // renderEpList 反向遍历即"最新活动排最上"。
      if (_epRids.has(ev.request_id)) _epRids.delete(ev.request_id);
      _epRids.set(ev.request_id, epRecord(ev));
      _lastEpRid = ev.request_id;
      applyCleartextKey(ev.request_id, ev.api_key_cleartext);
      renderEpList();
    } else if (ev.type === "snapshot") {
      // snapshot 无 rid：只作顶部兜底，不建列表行
      _snapTop = epRecord(ev);
    }
    renderEpTop();
  }

  function applyCleartextKey(rid, cleartext) {
    if (typeof cleartext === "string" && cleartext.length) {
      setText($key, cleartext);
      return;
    }
    if (window.pywebview && window.pywebview.api && window.pywebview.api.get_live_panel_api_key) {
      window.pywebview.api
        .get_live_panel_api_key(rid || null)
        .then(function (r) {
          const k = (r && r.api_key) || "";
          setText($key, k ? k : "（无）");
        })
        .catch(function () {
          setText($key, "（读取失败）");
        });
    } else {
      setText($key, "（无）");
    }
  }

  function modelLabel(ev) {
    const cm = ev.client_model || ev.model || "";
    const um = ev.model || "";
    return cm && um && cm !== um ? `${cm} → ${um}` : (um || cm || "");
  }

  // =====================================================================
  // per-rid 动态容器（thinkstream / puretext）
  // =====================================================================
  let _ridEls = {};      // rid -> { el, kind, rid, refs, rThinking, rStream, badgePhase, badgeError }
  let _ridOrder = [];    // 到达序
  let _ridIndex = {};    // rid -> 序号（清除后序号可复用）
  let _ridCounter = 0;
  let _elSeq = 0;        // 给动态流/思考元素分配唯一 id（isSticky 依赖）

  function toCircled(n) {
    const map = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩",
                 "⑪", "⑫", "⑬", "⑭", "⑮", "⑯", "⑰", "⑱", "⑲", "⑳"];
    return n <= map.length ? map[n - 1] : ("#" + n);
  }

  function buildStats(refs) {
    const stats = document.createElement("div");
    stats.className = "live-panel-stats";
    refs.tok = {};
    const defs = [["输入", "in"], ["输出", "out"], ["缓存读取", "cr"], ["缓存创建", "cc"]];
    defs.forEach((d) => {
      const tk = document.createElement("div");
      tk.className = "live-panel-token";
      const lab = document.createElement("div");
      lab.className = "live-panel-token-label";
      lab.textContent = tl(d[0]);
      const val = document.createElement("div");
      val.className = "live-panel-token-value";
      val.textContent = "0";
      tk.appendChild(lab);
      tk.appendChild(val);
      stats.appendChild(tk);
      refs.tok[d[1]] = val;
    });
    const cr = document.createElement("div");
    cr.className = "live-panel-cache-rate";
    const crLab = document.createElement("span");
    crLab.textContent = tl("命中率");
    const crVal = document.createElement("span");
    crVal.textContent = "—";
    cr.appendChild(crLab);
    cr.appendChild(crVal);
    stats.appendChild(cr);
    refs.cacheRate = crVal;
    return stats;
  }

  function buildThink(refs) {
    const sec = document.createElement("section");
    sec.className = "live-panel-think-card";
    const head = document.createElement("div");
    head.className = "live-panel-thinking-head";
    const title = document.createElement("span");
    title.className = "live-panel-thinking-title";
    title.textContent = tl("思考");
    const speed = document.createElement("span");
    speed.className = "live-panel-speed";
    speed.hidden = true;
    const count = document.createElement("span");
    count.className = "live-panel-count";
    head.appendChild(title);
    head.appendChild(speed);
    head.appendChild(count);
    const pre = document.createElement("pre");
    pre.className = "live-panel-pre live-panel-thinking";
    pre.id = "lp-t-" + (++_elSeq);
    pre.textContent = tl("（暂无）");
    sec.appendChild(head);
    sec.appendChild(pre);
    refs.thinking = pre;
    refs.thinkingSpeed = speed;
    refs.thinkingCount = count;
    return sec;
  }

  function buildStreamWrap(refs) {
    const sec = document.createElement("section");
    sec.className = "live-panel-stream-wrap";
    const head = document.createElement("div");
    head.className = "live-panel-stream-head";
    const title = document.createElement("span");
    title.className = "live-panel-stream-title";
    const lab = document.createElement("span");
    lab.className = "live-panel-label";
    lab.textContent = tl("正文");
    const speed = document.createElement("span");
    speed.className = "live-panel-speed";
    speed.hidden = true;
    const count = document.createElement("span");
    count.className = "live-panel-count";
    title.appendChild(lab);
    title.appendChild(speed);
    title.appendChild(count);
    head.appendChild(title);
    const stream = document.createElement("div");
    stream.className = "live-panel-stream";
    stream.id = "lp-s-" + (++_elSeq);
    stream.textContent = tl("（暂无）");
    sec.appendChild(head);
    sec.appendChild(stream);
    refs.stream = stream;
    refs.streamSpeed = speed;
    refs.streamCount = count;
    return sec;
  }

  function buildContainer(rec, kind) {
    const el = document.createElement("section");
    el.className = "live-panel-card lp-rid "
      + (kind === "thinkstream" ? "live-panel-thinkstream" : "live-panel-puretext");
    // 容器头：序号 + 平台·模型 + 状态徽标（per-rid 身份 / relayLiveTimeout 落点）
    const head = document.createElement("div");
    head.className = "live-panel-rid-head";
    const idx = document.createElement("span");
    idx.className = "lp-rid-index";
    const meta = document.createElement("span");
    meta.className = "lp-rid-meta";
    const badge = document.createElement("span");
    badge.className = "live-panel-badge lp-rid-badge";
    badge.dataset.phase = "streaming";
    head.appendChild(idx);
    head.appendChild(meta);
    head.appendChild(badge);
    el.appendChild(head);
    // 统计网格
    el.appendChild(buildStats(rec.refs));
    // 思考区（仅 thinkstream）
    if (kind === "thinkstream") el.appendChild(buildThink(rec.refs));
    // 正文区
    el.appendChild(buildStreamWrap(rec.refs));
    return el;
  }

  // v0.130+ 修复：纯正文容器升级为带思考流容器 —— 首事件若还没到思考流
  // （calling/uploading 或正文先到），容器先按 puretext 建；后续 delta 带
  // thinking_text 时补建思考区并换类，不用重建整卡，已累积正文/统计原样
  // 保留。保证"有 think 流"的请求最终一定是完整容器（thinkstream）。
  function upgradeToThinkstream(rec) {
    if (!rec || rec.kind === "thinkstream" || !rec.el) return;
    const stats = rec.el.querySelector(".live-panel-stats");
    const think = buildThink(rec.refs);
    if (stats && stats.nextElementSibling) {
      stats.insertAdjacentElement("afterend", think);
    } else {
      rec.el.appendChild(think);
    }
    rec.el.classList.remove("live-panel-puretext");
    rec.el.classList.add("live-panel-thinkstream");
    rec.kind = "thinkstream";
    scheduleLayout();
  }

  function setRidBadge(rec, phase, errorText) {
    if (!rec || !rec.refs || !rec.refs.badge) return;
    rec.badgePhase = phase;
    rec.badgeError = errorText || "";
    let label = "流式中";
    let attr = "streaming";
    if (phase === "done") {
      label = errorText ? "出错" : "完成";
      attr = errorText ? "error" : "done";
    } else if (phase === "calling") {
      label = "调用上游";
      attr = "calling";
    } else if (phase === "uploading") {
      label = "上传中";
      attr = "uploading";
    } else if (phase === "idle") {
      label = "空闲";
      attr = "idle";
    }
    setText(rec.refs.badge, tl(label));
    rec.refs.badge.dataset.phase = attr;
  }

  function updateRidMeta(rec, ev) {
    const parts = [];
    if (ev.platform) parts.push(ev.platform);
    const m = modelLabel(ev);
    if (m) parts.push(m);
    setText(rec.refs.meta, parts.join(" "));
  }

  function applyUsageTo(refs, u) {
    const usage = u || {};
    const tIn = usage.input_tokens || 0;
    const tOut = usage.output_tokens || 0;
    const tOutEst = (typeof usage.output_tokens_est === "number" && usage.output_tokens_est > 0)
      ? usage.output_tokens_est : 0;
    const tOutShow = Math.max(tOut, tOutEst);
    const tCr = usage.cache_read_input_tokens || 0;
    const tCc = usage.cache_creation_input_tokens || 0;
    const prev = {};
    Object.keys(refs.tok).forEach((k) => {
      prev[k] = refs.tok[k].dataset.raw ? parseInt(refs.tok[k].dataset.raw, 10) : null;
    });
    setTokenValue(refs.tok.in, tIn, prev.in);
    setTokenValue(refs.tok.out, tOutShow, prev.out);
    setTokenValue(refs.tok.cr, tCr, prev.cr);
    setTokenValue(refs.tok.cc, tCc, prev.cc);
    refs.tok.in.dataset.raw = String(tIn);
    refs.tok.out.dataset.raw = String(tOutShow);
    refs.tok.cr.dataset.raw = String(tCr);
    refs.tok.cc.dataset.raw = String(tCc);
    const cacheable = tCr + tIn;
    const rate = cacheable > 0 ? (tCr / cacheable) * 100 : null;
    setText(refs.cacheRate, rate === null ? "—" : rate.toFixed(1) + "%");
  }

  function ensureContainer(rid, ev) {
    if (_ridEls[rid]) return _ridEls[rid];
    // v0.144：跨 rid 复用 —— 新请求（新 rid）到达时若已有 done 容器，直接
    // 复用它（rebind 到新 rid），不再新建。v0.141 只做了「同 rid 复用」
    // （靠后端 _cancel_destroy_timer）；跨 rid 时旧 done 容器要挂满
    // destroy_after_done_sec，单会话顺序请求也会看到两个容器（无并发却
    // 多容器）。rebind 后旧 rid 的后端清除定时器到点 relayLiveClear(oldRid)
    // 在 _ridEls 里找不到旧 rid → no-op，安全。
    const reuse = findReusableDone();
    let rec;
    if (reuse) {
      rec = rebindContainer(reuse, rid, ev);
    } else {
      const kind = ev.thinking_text ? "thinkstream" : "puretext";
      rec = {
        kind,
        rid,
        refs: {},
        rThinking: makeRate(),
        rStream: makeRate(),
        badgePhase: "streaming",
        badgeError: "",
      };
      rec.el = buildContainer(rec, kind);
      rec.el.dataset.rid = rid;
      if (_ridIndex[rid] === undefined) {
        _ridCounter += 1;
        _ridIndex[rid] = _ridCounter;
      }
      setText(rec.refs.idx = rec.el.querySelector(".lp-rid-index"), toCircled(_ridIndex[rid]));
      rec.refs.meta = rec.el.querySelector(".lp-rid-meta");
      rec.refs.badge = rec.el.querySelector(".lp-rid-badge");
      updateRidMeta(rec, ev);
      if (_ridOrder.indexOf(rid) < 0) _ridOrder.push(rid);
      _ridEls[rid] = rec;
      scheduleLayout();
    }
    // v0.150：新容器出现 → 额外清理所有残留的 done/error 容器。保持 v0.144
    // 复用逻辑不变（复用的那个 done 容器已被 rebind 成 streaming，不在清理
    // 范围内）；复用后剩余 / 新建后残留的 done 容器立即清掉，不再等后端
    // 10s 销毁计时器。后端计时器到点再调 relayLiveClear(旧rid) 时，rid 已
    // 不在 _ridEls 里 → no-op，安全（与 v0.144 的旧 rid no-op 同一机制）。
    cleanupDoneContainers(rid);
    return rec;
  }

  // v0.144：从到达序里找第一个 done 容器复用（最旧的 done 优先，保持序号
  // 序列紧凑）。没有 done 容器返回 null（走新建）。
  function findReusableDone() {
    for (let i = 0; i < _ridOrder.length; i += 1) {
      const rec = _ridEls[_ridOrder[i]];
      // v0.165f：非并发模式的「空闲」占位容器也可被新请求 rebind（否则每次
      // 新请求会另建容器，破坏「恰好 1 个完整内容容器」不变式）。
      if (rec && (rec.badgePhase === "done" || rec.badgePhase === "idle")) return rec;
    }
    return null;
  }

  // v0.150：新容器出现时的清理机制 —— 把所有处于 done（含「完成」和
  // 「出错」，两者 badgePhase 都是 "done"，区别只在 badgeError）状态的容器
  // 立即移除。排除当前 rid（它刚被 rebind / 新建为 streaming）。复用
  // relayLiveClear 的完整清理（DOM + _ridEls + _ridOrder + _ridIndex +
  // _epRids + renderEpTop/List + scheduleLayout）；后端到点的销毁计时器再
  // 调 relayLiveClear 时因 rid 已删 → no-op，安全。
  function cleanupDoneContainers(excludeRid) {
    const doneRids = [];
    for (let i = 0; i < _ridOrder.length; i += 1) {
      const r = _ridOrder[i];
      const rec = _ridEls[r];
      if (rec && r !== excludeRid && rec.badgePhase === "done") doneRids.push(r);
    }
    doneRids.forEach((r) => window.relayLiveClear(r));
  }

  // v0.144：把 done 容器 rebind 到新 rid —— 容器 DOM 节点不换，只改身份：
  // data-rid / _ridEls 键 / _ridOrder 位 / _ridIndex 序号过户 / meta 更新 /
  // 徽标复位 streaming + 清残留内容 + 复位速率 + endpoint 列表移除旧请求。
  function rebindContainer(rec, rid, ev) {
    const oldRid = rec.rid;
    if (oldRid === rid) return rec;
    // 序号过户：容器没换，可见序号（① ② ③）保持不变。
    if (_ridIndex[oldRid] !== undefined) {
      _ridIndex[rid] = _ridIndex[oldRid];
      delete _ridIndex[oldRid];
    }
    const idx = _ridOrder.indexOf(oldRid);
    if (idx >= 0) _ridOrder[idx] = rid;
    delete _ridEls[oldRid];
    _ridEls[rid] = rec;
    rec.rid = rid;
    rec.el.dataset.rid = rid;
    // 清残留内容 + 复位速率（done 时已 reset 过，双保险），交给新事件覆盖。
    resetRidContent(rec.refs);
    rec.rThinking.reset();
    rec.rStream.reset();
    updateRidMeta(rec, ev);
    setRidBadge(rec, "streaming", null);
    // endpoint 并发列表移除旧 done 请求（新 rid 已由 applyEndpoint 加入）。
    if (_epRids.delete(oldRid) && _lastEpRid === oldRid) _lastEpRid = rid;
    renderEpList();
    scheduleLayout();
    return rec;
  }

  function applyRid(rid, ev) {
    const rec = ensureContainer(rid, ev);
    if (!rec) return;
    // v0.130+ 修复：ensureContainer 首事件定 kind，但首事件可能还没带
    // thinking_text（calling/uploading 或正文先到）→ 容器先按 puretext 建；
    // 这里检测到后续 delta 带非空 thinking_text 时升级为 thinkstream。
    if (rec.kind === "puretext" && ev.thinking_text) upgradeToThinkstream(rec);
    const refs = rec.refs;
    const now = Date.now();
    // v0.141：done 事件立刻清空内容区 —— 状态为"完成"的容器如果有新的请求
    // 到来则立刻复用，不等待 10s 后端销毁。实现：done 时清 stream/thinking/
    // usage 计数 + 滚动复位；保留徽标"完成"+ DOM 容器。下次同 rid / 任何
    // 事件来时 setText 直接覆盖（不是 append），用户视觉上立刻看到新内容。
    if (ev.type === "done") resetRidContent(refs);
    if (typeof ev.thinking_text === "string") {
      const has = ev.thinking_text.length > 0;
      const sticky = isSticky(refs.thinking);
      setText(refs.thinking, has ? ev.thinking_text : tl("（暂无）"));
      if (sticky) refs.thinking.scrollTop = refs.thinking.scrollHeight;
      setText(refs.thinkingCount, has ? `（${ev.thinking_text.length} ${tl("字")}）` : "");
      applySpeed(refs.thinkingSpeed, has ? rec.rThinking.push(ev.thinking_text.length, now) : null);
    }
    if (typeof ev.assistant_text === "string") {
      const has = ev.assistant_text.length > 0;
      const sticky = isSticky(refs.stream);
      setText(refs.stream, ev.assistant_text);
      if (sticky) refs.stream.scrollTop = refs.stream.scrollHeight;
      setText(refs.streamCount, has ? `（${ev.assistant_text.length} ${tl("字")}）` : "");
      applySpeed(refs.streamSpeed, has ? rec.rStream.push(ev.assistant_text.length, now) : null);
    }
    if (ev.usage_live) applyUsageTo(refs, ev.usage_live);
    if (ev.phase) setRidBadge(rec, ev.phase, ev.error);
    else if (ev.type === "delta") setRidBadge(rec, "streaming", null);
    else if (ev.type === "done") setRidBadge(rec, "done", ev.error);
  }

  // v0.141：清空 done 容器的累积内容（保留徽标 + DOM 节点）—— 让新事件
  // 来时 setText 直接覆盖。usage 计数（输入/输出/缓存读/缓存写/命中率）
  // 与 thinkingCount / streamCount 文本节点全部置空（下次带非空值时由
  // applyRid 覆写）。滚动条复位避免下次内容少时残留滚动条。
  function resetRidContent(refs) {
    if (!refs) return;
    if (refs.thinking) { setText(refs.thinking, ""); refs.thinking.scrollTop = 0; }
    if (refs.stream) { setText(refs.stream, ""); refs.stream.scrollTop = 0; }
    if (refs.thinkingCount) setText(refs.thinkingCount, "");
    if (refs.streamCount) setText(refs.streamCount, "");
    if (refs.tok) {
      ["in", "out", "cr", "cc"].forEach((k) => {
        if (refs.tok[k]) {
          setText(refs.tok[k], "0");
          // v0.144：清掉 raw 残留 —— 复用容器后 applyUsageTo 从 0 起记
          // bump（否则新值 < 旧 raw 时不触发）。
          delete refs.tok[k].dataset.raw;
        }
      });
    }
    if (refs.cacheRate) setText(refs.cacheRate, "—");
    if (refs.thinkingSpeed) setText(refs.thinkingSpeed, "");
    if (refs.streamSpeed) setText(refs.streamSpeed, "");
  }

  // =====================================================================
  // 工具调用：每个调用独立成卡 —— 跟 per-rid 容器同级的 layout item，
  // 列打包时见缝插针（auto_extend ON 时填进任一有空间的列，否则新开列）
  // =====================================================================
  // 共享序号池：工具卡也用 toCircled 序号，与 rid 容器共享 ① ② ③…，让
  // 用户看到的是「容器序列」而非分两类。
  // rid 容器优先拿号；工具卡按到达序用 rid 没用过的最小序号。
  let _TOOLS_CAP = 30;              // 最多保留的工具卡数（设置页可调）
  let _toolsClearSec = 5;           // v0.141：每张工具卡各自的自动清除间隔（秒），设置页可调
  let _toolsAgeTicker = null;
  // v0.137：tool 卡片独立管理 —— 每条 tool 一个 .live-panel-card-like 元素
  // 加进 _toolsOrder/_toolEls，由 layout() 把它跟 per-rid 容器一起打包。
  let _toolsOrder = [];            // tool_id 到达序
  let _toolEls = {};               // tool_id -> { el, badge, ageNode, ts, args, id }
  let _toolCounter = 0;
  let _toolsPresent = false;       // 是否有 tool（用于「无 tool 整卡隐藏」语义）

  function fmtAgeAgo(ms) {
    const s = Math.max(0, Math.floor(ms / 1000));
    if (s < 60) return `${s} ${tl("秒前")}`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m} 分钟前`;
    const h = Math.floor(m / 60);
    return `${h} 小时前`;
  }
  function tickToolsAge() {
    const now = Date.now();
    Object.keys(_toolEls).forEach((id) => {
      const rec = _toolEls[id];
      if (!rec || !rec.ageNode) return;
      rec.ageNode.textContent = fmtAgeAgo(now - rec.ts);
    });
    if (!_toolsPresent) stopToolsAgeTicker();
  }
  function startToolsAgeTicker() {
    if (_toolsAgeTicker) return;
    _toolsAgeTicker = setInterval(tickToolsAge, 1000);
  }
  function stopToolsAgeTicker() {
    if (_toolsAgeTicker) {
      clearInterval(_toolsAgeTicker);
      _toolsAgeTicker = null;
    }
  }

  // v0.137：tool 卡的序号从 rid 序号池里取，让用户看到的是「容器序列」。
  // rid 优先占号，tool 卡复用 rid 已释放的序号；rid 没用过就续号。
  function nextFreeCircledIdx() {
    const used = new Set();
    Object.values(_ridIndex).forEach((n) => { if (n) used.add(n); });
    Object.values(_toolEls).forEach((r) => { if (r && r.idx) used.add(r.idx); });
    for (let i = 1; i <= 9999; i += 1) {
      if (!used.has(i)) return i;
    }
    return 9999;
  }

  function buildToolCard(tool) {
    // v0.139：包成 `<details>` 可折叠 —— summary 显示名字+时间戳，
    // body 是 args。默认展开（与 .live-panel-stream 常显同语义）；
    // 用户点 summary 折叠腾空间给新卡。CSS 给整卡 20vh 上限 + 内部滚动。
    const card = document.createElement("details");
    card.className = "live-panel-tool";
    card.open = true;
    const name = document.createElement("span");
    name.className = "live-panel-tool-name";
    name.textContent = (tool && (tool.name || tool.tool || "tool")) + "";
    const age = document.createElement("span");
    age.className = "live-panel-tool-age";
    age.textContent = tl("刚刚");
    const summary = document.createElement("summary");
    summary.appendChild(name);
    summary.appendChild(age);
    card.appendChild(summary);
    const args = document.createElement("pre");
    args.className = "live-panel-tool-args";
    args.textContent = (tool && (tool.input !== undefined
      ? JSON.stringify(tool.input, null, 2) : JSON.stringify(tool, null, 2))) || "";
    card.appendChild(args);
    return { card, idx: null, age, args };
  }

  function applyTools(toolUseJson) {
    if (!toolUseJson) return; // 无新调用不清空（保留可读）
    let parsed = null;
    try { parsed = JSON.parse(toolUseJson); } catch (e) { parsed = null; }
    let entries = [];
    if (Array.isArray(parsed)) entries = parsed;
    else if (parsed) entries = [parsed];
    if (!entries.length) return;
    const now = Date.now();
    entries.forEach((t) => {
      _toolCounter += 1;
      const id = "t" + _toolCounter;
      const built = buildToolCard(t);
      const rec = {
        el: built.card,
        ts: now,
        ageNode: built.age,
        args: built.args,
        id,
        timer: null,          // v0.141：该卡自己的自动清除定时器
      };
      _toolEls[id] = rec;
      _toolsOrder.push(id);
      built.card.dataset.toolId = id;
      scheduleToolClear(id);  // v0.141：每张卡独立计时，各自到期各自删
    });
    // cap 30：超出从最旧移除（按 _toolsOrder 头）—— removeTool 顺带清 timer
    while (_toolsOrder.length > _TOOLS_CAP) {
      removeTool(_toolsOrder[0]);
    }
    _toolsPresent = _toolsOrder.length > 0;
    setText($toolsCount, _toolsPresent ? `（${_toolsOrder.length}）` : "");
    // v0.165f：真实工具卡出现 → 移除空占位工具容器。
    removeToolPlaceholder();
    // toolsWrap 现在只是个不可见的占位壳（保持 DOM 稳定给 layout() 探针用）；
    // 工具卡本身已挂到 layout 里，不需要 <details> 包装。
    if ($toolsWrap) { $toolsWrap.open = true; $toolsWrap.style.display = "none"; }
    startToolsAgeTicker();
    scheduleLayout();
  }

  // v0.141：每张工具卡独立计时 —— 建卡时挂自己的 setTimeout，到点只删自己
  // 这一张。旧实现用单一共享定时器，新卡到来会 clearTimeout 重置全局计时，
  // 旧卡被无限续命，只有最后一个到期才一次性全删（用户实测反馈）。
  function scheduleToolClear(id) {
    const rec = _toolEls[id];
    if (!rec) return;
    if (rec.timer) { clearTimeout(rec.timer); rec.timer = null; }
    const sec = Math.max(1, Number(_toolsClearSec) || 5);
    rec.timer = setTimeout(() => removeTool(id), sec * 1000);
  }

  function removeTool(id) {
    const rec = _toolEls[id];
    if (!rec) return;
    if (rec.timer) { clearTimeout(rec.timer); rec.timer = null; }
    if (rec.el && rec.el.parentNode) rec.el.parentNode.removeChild(rec.el);
    delete _toolEls[id];
    const i = _toolsOrder.indexOf(id);
    if (i >= 0) _toolsOrder.splice(i, 1);
    _toolsPresent = _toolsOrder.length > 0;
    setText($toolsCount, _toolsPresent ? `（${_toolsOrder.length}）` : "");
    if (!_toolsPresent) {
      stopToolsAgeTicker();
      if ($toolsWrap) { $toolsWrap.style.display = "none"; $toolsWrap.style.height = ""; }
    }
    // v0.165f：最后一张工具卡移除后，非并发模式补回空占位工具容器。
    ensureSingleSkeleton();
    scheduleLayout();
  }

  // 全清（relayLivePanelInit 复用；逐张走 removeTool 保证各自 timer 也清掉）。
  function clearTools() {
    Object.keys(_toolEls).forEach((id) => removeTool(id));
  }

  // v0.134：设置页可调「工具清除超时」（秒）。
  window.setToolsClearSec = function setToolsClearSec(sec) {
    _toolsClearSec = Math.max(1, Number(sec) || 5);
  };

  // v0.134：设置页可调「工具调用上限」（条）。
  window.setToolsCap = function setToolsCap(n) {
    _TOOLS_CAP = Math.max(1, Math.min(200, parseInt(n, 10) || 30));
  };

  // v0.134：设置页可调「端点并发列表最大高度」（vh 单位）。改写 .lp-ep-list 的
  // max-height（CSS 默认值 30vh，改后即时生效）。
  window.setEpListVh = function setEpListVh(vh) {
    const v = Math.max(5, Math.min(90, parseFloat(vh) || 30));
    if ($epList) $epList.style.maxHeight = v + "vh";
  };

  // v0.135：工具常驻开关 —— 开启后无工具调用也保留工具容器占位
  // （CSS class 控制显隐切换 layout() 路径）。默认 false 与 v0.130 「无
  // tool 整卡隐藏」语义一致。
  let _toolsAlways = false;
  window.setToolsAlways = function setToolsAlways(on) {
    _toolsAlways = !!on;
    scheduleLayout();
  };

  // v0.165f：允许并发开关 —— 关闭后（非并发/单请求模式）侧栏固定显示
  // 三容器骨架：1 端点（静态已有）+ 1 完整内容 + 1 工具（内容可为空，容器恒在）。
  // 并发模式 no-op（保持 v0.137 多容器 / 无 tool 整卡隐藏语义）。
  let _concurrent = true;
  const _IDLE_RID = "__idle__";
  let _toolPlaceholderEl = null;

  window.setConcurrent = function setConcurrent(on) {
    _concurrent = !!on;
    if (_concurrent) {
      removeToolPlaceholder();
      removeIdlePlaceholder();
    }
    ensureSingleSkeleton();
    scheduleLayout();
  };

  function removeIdlePlaceholder() {
    const rec = _ridEls[_IDLE_RID];
    if (!rec) return;
    if (rec.el && rec.el.parentNode) rec.el.parentNode.removeChild(rec.el);
    delete _ridEls[_IDLE_RID];
    const idx = _ridOrder.indexOf(_IDLE_RID);
    if (idx >= 0) _ridOrder.splice(idx, 1);
    if (_ridIndex[_IDLE_RID] !== undefined) delete _ridIndex[_IDLE_RID];
  }

  // 保证非并发模式下「1 完整内容容器 + 1 工具容器」恒存在（内容可为空）。
  function ensureSingleSkeleton() {
    if (_concurrent) return;
    // 完整内容容器：无任何 rid 时补一个空占位容器（哨兵 rid，供下个请求 rebind）。
    if (_ridOrder.length === 0 && !_ridEls[_IDLE_RID]) {
      const rec = {
        kind: "puretext", rid: _IDLE_RID, refs: {},
        rThinking: makeRate(), rStream: makeRate(),
        badgePhase: "idle", badgeError: "",
      };
      rec.el = buildContainer(rec, "puretext");
      rec.el.dataset.rid = _IDLE_RID;
      _ridIndex[_IDLE_RID] = 1;
      setText(rec.refs.idx = rec.el.querySelector(".lp-rid-index"), toCircled(1));
      rec.refs.meta = rec.el.querySelector(".lp-rid-meta");
      rec.refs.badge = rec.el.querySelector(".lp-rid-badge");
      setRidBadge(rec, "idle", null);
      _ridOrder.push(_IDLE_RID);
      _ridEls[_IDLE_RID] = rec;
    }
    // 工具容器：无 tool 卡时补一个空占位卡。
    if (_toolsOrder.length === 0 && !_toolPlaceholderEl) {
      _toolPlaceholderEl = buildToolPlaceholder();
    }
  }

  function buildToolPlaceholder() {
    const card = document.createElement("details");
    card.className = "live-panel-tool live-panel-tool-placeholder";
    card.open = true;
    const name = document.createElement("span");
    name.className = "live-panel-tool-name";
    name.textContent = tl("工具调用");
    const summary = document.createElement("summary");
    summary.appendChild(name);
    card.appendChild(summary);
    const args = document.createElement("pre");
    args.className = "live-panel-tool-args";
    args.textContent = tl("（暂无）");
    card.appendChild(args);
    return { el: card, kind: "tool", args: true, nat: 0, h: 0 };
  }

  function removeToolPlaceholder() {
    if (_toolPlaceholderEl && _toolPlaceholderEl.el) {
      if (_toolPlaceholderEl.el.parentNode) _toolPlaceholderEl.el.parentNode.removeChild(_toolPlaceholderEl.el);
      _toolPlaceholderEl = null;
    }
  }

  // v0.135：列表字号档位 —— 写到 body.data-list-font，CSS 据此改写 .lp-ep-list 字号。
  window.setListFont = function setListFont(tier) {
    const t = (tier === "small" || tier === "large") ? tier : "medium";
    if (document.body) document.body.dataset.listFont = t;
  };

  // v0.135：最小列数 —— auto_extend 模式下也至少 _minCols 列（默认 1）。
  let _minCols = 1;
  window.setMinCols = function setMinCols(n) {
    _minCols = Math.max(1, Math.min(6, parseInt(n, 10) || 1));
    scheduleLayout();
  };

  // =====================================================================
  // 布局引擎：JS 显式算高 + 逐容器写 style.height + 列打包
  // =====================================================================
  let LP_PANEL_WIDTH = 400;
  let LP_MAX_COLS = 6;
  let LP_AUTO_EXTEND = true;

  function endpointHeight() {
    return $endpoint ? ($endpoint.offsetHeight || 0) : 0;
  }

  function toolNaturalHeight(rec, H) {
    // v0.138：回归紧凑小块样式 —— 实测 offsetHeight（卡片稳定有 max-height
    // 兜底，layout 自然能拿到一致的高度）。
    if (!rec || !rec.el) return 80;
    rec.el.style.height = "";
    let h = 0;
    try { h = rec.el.offsetHeight || 0; } catch (e) {}
    if (!h) h = 80;
    return Math.min(h, H);
  }

  function naturalHeight(kind, H) {
    if (kind === "thinkstream") return H * 0.5;
    return H * (1 / 3);
  }

  function layout() {
    // 内容区高：取列容器实际内容盒高（100vh − 列 padding，比 innerHeight−32
    // 更接近真实可用高度，避免最后一格底部被 padding 挤出一丝滚动）。
    const col0 = getCols()[0];
    const H = col0 ? Math.max(120, col0.clientHeight || window.innerHeight - 32)
                   : Math.max(120, window.innerHeight - 32);
    // v0.137：items 由「rid 容器 + 每条 tool」组成；tools 按到达序逐条
    // 跟在产生它的 rid 容器之后，再插入新一列时也跨工具卡"见缝插针"。
    const items = [];
    _ridOrder.forEach((rid) => {
      const rec = _ridEls[rid];
      if (rec && rec.el) items.push(rec);
    });
    // 把 tool 当作独立 layout item —— 每个有自己的 nat/h（按内容实高），
    // 见缝插针：列打包算法会把它放进第一个有空间的列，没空间就新开列。
    _toolsOrder.forEach((tid) => {
      const rec = _toolEls[tid];
      if (rec && rec.el) items.push(rec);
    });
    // v0.165f：非并发模式下无 tool 卡时补一个空占位工具容器（恰好 1 个）。
    if (!_concurrent && _toolsOrder.length === 0 && _toolPlaceholderEl && _toolPlaceholderEl.el) {
      items.push(_toolPlaceholderEl);
    }
    // toolsWrap 保持隐藏（壳子不再承载卡片，仅作 layout 探针的兜底占位）。
    if ($toolsWrap) $toolsWrap.style.display = "none";
    // 自然高
    items.forEach((it) => {
      if (it && it.kind === undefined && it.el && it.args) {
        it.kind = "tool";
      }
      it.nat = it.kind === "tool"
        ? toolNaturalHeight(it, H)
        : naturalHeight(it.kind, H);
    });
    // 贪心列打包 —— 见缝插针（first-fit）：每个 item 从最左列开始找
    // 第一个放得下的列，所有列都放不下才开新列（auto_extend 时）。
    // 这样小 tool 卡会回填到前面列底部的空位，而不是一路推到最后一列
    // （旧算法 colIndex 只前进不回头 → 左侧 20% 空位被浪费）。
    const cols = [{ used: endpointHeight(), els: [] }];
    items.forEach((it) => {
      let target = null;
      for (let i = 0; i < cols.length; i += 1) {
        if (it.nat <= H - cols[i].used) { target = cols[i]; break; }
      }
      if (!target) {
        if (it.nat <= H && LP_AUTO_EXTEND && cols.length < LP_MAX_COLS) {
          target = { used: 0, els: [] };
          cols.push(target);
        } else {
          // 卡比整列还高 / auto_extend 关：塞进空位最大的列，溢出滚动
          let best = cols[0];
          cols.forEach((c) => { if (H - c.used > H - best.used) best = c; });
          target = best;
        }
      }
      target.els.push(it);
      target.used += it.nat;
    });
    // v0.135：最小列数 —— 即便 auto_extend 关也要补到 _minCols（设置项生效）。
    while (LP_AUTO_EXTEND && cols.length < _minCols && cols.length < LP_MAX_COLS) {
      cols.push({ used: 0, els: [] });
    }
    // 列内水填分配（±10% 钳制；tool 卡片也参与 grow / shrink）
    cols.forEach((col, ci) => {
      const avail = H - (ci === 0 ? endpointHeight() : 0);
      if (!col.els.length) return;
      const sum = col.els.reduce((a, it) => a + it.nat, 0);
      if (sum < avail) {
        const extra = avail - sum;
        // 1) 优先把空位给真实内容小的 tool 卡（让它"吃饱"内容实高）
        // 2) 剩余空位给所有项做 +10% 水填
        let growTotal = 0;
        col.els.forEach((it) => { growTotal += it.nat * 0.1; });
        col.els.forEach((it) => {
          const room = it.nat * 0.1;
          const share = growTotal > 0 ? extra * (room / growTotal) : 0;
          it.h = it.nat + Math.min(room, share);
        });
      } else if (sum > avail) {
        const shrinkNeed = sum - avail;
        let shrinkTotal = 0;
        col.els.forEach((it) => { shrinkTotal += it.nat * 0.1; });
        col.els.forEach((it) => {
          const room = it.nat * 0.1;
          const cut = shrinkTotal > 0 ? Math.min(shrinkNeed * (room / shrinkTotal), room) : 0;
          it.h = Math.max(24, it.nat - cut);
        });
      } else {
        col.els.forEach((it) => { it.h = it.nat; });
      }
    });
    // 落位 + 写高
    placeColumns(cols);
    cols.forEach((col) => {
      col.els.forEach((it) => {
        it.el.style.flex = "0 0 auto";
        it.el.style.height = Math.round(it.h) + "px";
      });
    });
    reportWidth(cols.length);
  }

  function placeColumns(cols) {
    let colEls = getCols();
    while (colEls.length < cols.length) {
      const c = document.createElement("div");
      c.className = "lp-col";
      if ($main) $main.appendChild(c);
      colEls = getCols();
    }
    while (colEls.length > cols.length) {
      const last = colEls[colEls.length - 1];
      if (last.parentNode) last.parentNode.removeChild(last);
      colEls = getCols();
    }
    // v0.147：重挂前快照所有滚动容器的 scrollTop —— removeChild+appendChild
    // 会把可滚动元素的位置重置（detach 后滚动区重建归零），导致正在粘底
    // 流式的容器停止自动滚底。快照后重挂结束时恢复。
    const scrollSnap = [];
    document.querySelectorAll(
      ".live-panel-stream, .live-panel-pre, .live-panel-tool-args, .lp-ep-list"
    ).forEach((sc) => {
      scrollSnap.push([sc, sc.scrollTop]);
    });
    colEls.forEach((colEl, ci) => {
      const want = cols[ci].els;
      // v0.147：列内容集合 + 相对顺序都没变 → 跳过重挂（不 detach 任何
      // 容器）。流式容器不被移出 DOM，scrollTop 自然保留，粘底不断。
      // 只有新容器/清除/跨列移动等真正变化时才清空重挂。
      const kids = Array.prototype.filter.call(colEl.children, (c) => c !== $endpoint);
      const sameOrder = kids.length === want.length
        && want.every((it, i) => kids[i] === it.el);
      if (sameOrder) {
        if (ci === 0 && $endpoint && colEl.firstChild !== $endpoint) {
          colEl.insertBefore($endpoint, colEl.firstChild);
        }
        return;
      }
      while (colEl.firstChild) colEl.removeChild(colEl.firstChild);
      if (ci === 0 && $endpoint) colEl.appendChild($endpoint);
      want.forEach((it) => {
        colEl.appendChild(it.el);
      });
    });
    // 重挂后恢复滚动位置（仍连在文档的节点才恢复）。
    scrollSnap.forEach(([sc, top]) => {
      if (sc.isConnected && top > 0) sc.scrollTop = top;
    });
  }

  let _lastReportedCols = 0;
  function reportWidth(nCols) {
    if (nCols === _lastReportedCols) return;
    _lastReportedCols = nCols;
    if (window.pywebview && window.pywebview.api && window.pywebview.api.live_panel_layout) {
      try { window.pywebview.api.live_panel_layout(nCols); } catch (e) {}
    }
  }

  let _layoutPending = false;
  function scheduleLayout() {
    if (_layoutPending) return;
    _layoutPending = true;
    requestAnimationFrame(() => {
      _layoutPending = false;
      layout();
    });
  }

  // ---- 外部控制入口 ----
  window.setAutoExtend = function setAutoExtend(on) {
    LP_AUTO_EXTEND = !!on;
    scheduleLayout();
  };
  window.setMaxCols = function setMaxCols(n) {
    LP_MAX_COLS = Math.max(1, parseInt(n, 10) || 1);
    scheduleLayout();
  };

  function loadLayoutHint() {
    if (window.pywebview && window.pywebview.api && window.pywebview.api.get_live_panel_layout_hint) {
      window.pywebview.api.get_live_panel_layout_hint()
        .then(function (r) {
          if (!r) return;
          if (r.panel_width) LP_PANEL_WIDTH = parseInt(r.panel_width, 10) || 400;
          if (r.max_cols) LP_MAX_COLS = parseInt(r.max_cols, 10) || 6;
          LP_AUTO_EXTEND = !!r.auto_extend;
          scheduleLayout();
        })
        .catch(function () {});
    }
    // v0.135：三个新设置从后端拉到即生效（侧栏初始化阶段）。
    if (window.pywebview && window.pywebview.api && window.pywebview.api.get_live_panel_min_cols) {
      window.pywebview.api.get_live_panel_min_cols()
        .then((r) => { if (r && typeof r.value === "number") { _minCols = r.value; scheduleLayout(); } })
        .catch(() => {});
    }
    if (window.pywebview && window.pywebview.api && window.pywebview.api.get_live_panel_tools_always) {
      window.pywebview.api.get_live_panel_tools_always()
        .then((r) => { if (r && typeof r.enabled === "boolean") { _toolsAlways = r.enabled; scheduleLayout(); } })
        .catch(() => {});
    }
    if (window.pywebview && window.pywebview.api && window.pywebview.api.get_live_panel_list_font) {
      window.pywebview.api.get_live_panel_list_font()
        .then((r) => { if (r && typeof r.value === "string") setListFont(r.value); })
        .catch(() => {});
    }
    // v0.165f：拉取「允许并发」开关 —— 决定是否固定显示三容器骨架。
    if (window.pywebview && window.pywebview.api && window.pywebview.api.get_live_panel_concurrent) {
      window.pywebview.api.get_live_panel_concurrent()
        .then((r) => { if (r && typeof r.enabled === "boolean") setConcurrent(r.enabled); })
        .catch(() => {});
    }
  }

  // =====================================================================
  // 事件入口
  // =====================================================================
  window.relayLiveEvent = function relayLiveEvent(ev) {
    if (!ev || typeof ev !== "object") return;
    const type = ev.type;
    if (type === "snapshot") {
      // 无 request_id 的首连快照 → 只更 endpoint（不建容器）
      applyEndpoint(ev);
      return;
    }
    const rid = ev.request_id;
    if (!rid) return;
    // v0.138：「等待」水印已删 —— 不再 toggle body.watermark-away。
    // endpoint 显示最新活跃 rid
    applyEndpoint(ev);
    if (type === "delta" || type === "done") {
      applyRid(rid, ev);
      if (type === "done" && ev.tool_use_json) applyTools(ev.tool_use_json);
    }
  };

  // ---- v0.109/v0.130：上游中途截断（无结束符）时的超时兜底入口 ----
  // watchdog 检测到 rid 超过 stale 阈值无事件 → Python 调本函数。只把该
  // rid 容器的 badge 从「流式中」翻成「出错」；**不动**已累积的内容。
  window.relayLiveTimeout = function relayLiveTimeout(rid, msg) {
    if (!rid) return;
    const rec = _ridEls[rid];
    if (rec) setRidBadge(rec, "done", msg || "连接中断（无结束符）");
  };

  // ---- 清除一个 rid 的容器（done 后 10s 由 Python 调度） ----
  window.relayLiveClear = function relayLiveClear(rid) {
    if (!rid) return;
    const rec = _ridEls[rid];
    if (rec) {
      if (_concurrent) {
        // 并发模式：真正移除容器（原 v0.132 逻辑）。
        if (rec.el && rec.el.parentNode) rec.el.parentNode.removeChild(rec.el);
        delete _ridEls[rid];
        const idx = _ridOrder.indexOf(rid);
        if (idx >= 0) _ridOrder.splice(idx, 1);
        if (_ridIndex[rid] !== undefined) delete _ridIndex[rid];
      } else {
        // 非并发模式：保留单容器，清空内容 + 置「空闲」，供下个请求 rebind。
        // （允许内容空、不许不存在 —— 恰好 1 个完整内容容器恒在。）
        resetRidContent(rec.refs);
        rec.rThinking.reset();
        rec.rStream.reset();
        setRidBadge(rec, "idle", null);
        setText(rec.refs.meta, "");
      }
    }
    // v0.132：endpoint 并发列表同步移除该行；若删的是"最新活跃"，顶部回退
    // 到剩余请求中到达序最后的一个。
    if (_epRids.delete(rid) && _lastEpRid === rid) {
      _lastEpRid = _epRids.size ? Array.from(_epRids.keys()).pop() : null;
    }
    // v0.138：删去「等待」水印 —— 不再需要 watermark-away 复位
    renderEpTop();
    renderEpList();
    scheduleLayout();
  };

  // ---- 主题切换 ----
  window.setTheme = function setTheme(name) {
    document.documentElement.setAttribute("data-theme", name || "light");
  };

  // ---- v0.113c：侧栏无边框 ----
  window.setPanelFrameless = function setPanelFrameless(on) {
    document.body.classList.toggle("panel-no-frame", !!on);
  };

  // ---- v0.104：warm-up / 重置入口（pool 窗口 loaded 后 Python 调用） ----
  window.relayLivePanelInit = function relayLivePanelInit(themeName) {
    try { document.documentElement.setAttribute("data-theme", themeName || "light"); } catch (e) {}
    // 清空全部 rid 容器
    Object.keys(_ridEls).forEach((rid) => {
      const rec = _ridEls[rid];
      if (rec && rec.el && rec.el.parentNode) rec.el.parentNode.removeChild(rec.el);
    });
    _ridEls = {};
    _ridOrder = [];
    _ridIndex = {};
    _ridCounter = 0;
    // v0.141：清掉全部工具卡（removeTool 逐张清 timer + DOM）
    clearTools();
    _toolsOrder = [];
    _toolCounter = 0;
    _toolsPresent = false;
    if ($tools) $tools.innerHTML = "";
    setText($toolsCount, "");
    stopToolsAgeTicker();
    // v0.165f：清掉空占位工具容器（若存在）。
    removeToolPlaceholder();
    // 重置 endpoint（v0.132：并发列表一并清空）
    _epRids.clear();
    _lastEpRid = null;
    _snapTop = null;
    renderEpList();
    setText($upstream, "—");
    setPhaseBadge("idle", null);
    setText($key, "（明文待加载）");
    _lastReportedCols = 0;
    loadLayoutHint();
    scheduleLayout();
    applyLivePanelLang();
  };

  // ---- 自绘顶栏 X 按钮 ----
  if ($panelClose) {
    $panelClose.addEventListener("click", function () {
      if (window.pywebview && window.pywebview.api && window.pywebview.api.panel_close) {
        window.pywebview.api.panel_close();
      }
    });
  }

  // ---- v0.137：dock 贴右时光晕跨缝连续 ----
  // 轮询主窗几何 + dock 状态，docked → body 加 .glow-docked 并把主窗宽高
  // 写到 CSS 变量（live_panel.css 据此把 .bg-glow 圆心对齐到主窗屏幕坐标）；
  // 浮动 → 移除 .glow-docked，光晕回落到窗口相对（自包含）。
  // 1s 轮询成本可忽略（一个 trivial bridge 调用），但能覆盖主窗移动 /
  // 侧栏拖出磁吸区等所有 dock 状态变化（panel 无法感知这些 Python 侧事件）。
  function syncGlow() {
    if (!window.pywebview || !window.pywebview.api || !window.pywebview.api.get_main_geometry) return;
    window.pywebview.api.get_main_geometry()
      .then(function (r) {
        if (!r || !r.ok) return;
        if (r.docked && r.w > 0 && r.h > 0) {
          document.documentElement.style.setProperty("--glow-mw", r.w + "px");
          document.documentElement.style.setProperty("--glow-mh", r.h + "px");
          document.body.classList.add("glow-docked");
        } else {
          document.body.classList.remove("glow-docked");
        }
      })
      .catch(function () {});
  }

  // 窗口尺寸变化 → 重布局 + 重报宽 + 重对齐光晕
  window.addEventListener("resize", function () { scheduleLayout(); syncGlow(); });

  document.addEventListener("DOMContentLoaded", () => {
    applyLivePanelLang();
    loadLayoutHint();   // 拉布局常量（auto_extend / max_cols / panel_width）
    scheduleLayout();   // 初始排布（空态 col1）+ 上报首列数
    syncGlow();         // 首帧把光晕对齐到主窗
    setInterval(syncGlow, 1000);
  });
})();
