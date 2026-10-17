// Kanban 审计面板 —— desktop 插件(纯 ESM,被 Electron 渲染进程原样加载,不编译)
// 取数一律走 ctx.rest(path)(自动打到 /api/plugins/kanban-audit/*),轮询兜底 5s。
import { ROUTES_AREA, SIDEBAR_NAV_AREA, useQuery, fmtDateTime } from '@hermes/plugin-sdk'
import { useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const POLL_MS = 5000

// register 时注入,组件里统一用这个函数取数(见文件尾 register)
let rest = (path) => Promise.reject(new Error('插件尚未注册:' + path))

// ── 取值/格式化小工具(全部防 undefined)──────────────────────────────────────
const arr = (v) => (Array.isArray(v) ? v : [])
const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : 0)
const optNum = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null)
const pct1 = (v) => {
  const x = optNum(v)
  return x === null ? '—' : (x * 100).toFixed(1) + '%'
}
const fix1 = (v) => {
  const x = optNum(v)
  return x === null ? '—' : x.toFixed(1)
}
const money = (v) => {
  const x = optNum(v)
  return x === null ? '—' : '$' + x.toFixed(4)
}
const secs = (v) => {
  const x = optNum(v)
  return x === null ? '—' : x.toFixed(1) + 's'
}
const fmtTs = (v) => {
  if (v === null || v === undefined || v === '') return '—'
  const d = typeof v === 'number' ? new Date(v * 1000) : new Date(v)
  if (Number.isNaN(d.getTime())) return typeof v === 'string' ? v : '—'
  return fmtDateTime.format(d)
}
const errMsg = (e) => (e && e.message ? String(e.message) : String(e || '未知错误'))
const sevColor = (s) => (s >= 75 ? 'var(--ui-danger)' : s >= 50 ? 'var(--ui-orange)' : 'var(--ui-text-tertiary)')

// ── 通用样式(颜色只用主题变量)──────────────────────────────────────────────
const S = {
  header: {
    display: 'flex',
    alignItems: 'center',
    gap: '12px',
    padding: '10px 12px',
    borderBottom: '1px solid var(--ui-stroke-secondary)',
    flex: '0 0 auto'
  },
  warn: {
    padding: '8px 12px',
    color: 'var(--ui-orange)',
    borderBottom: '1px solid var(--ui-stroke-tertiary)',
    flex: '0 0 auto'
  },
  counts: {
    padding: '8px 12px',
    color: 'var(--ui-text-secondary)',
    fontSize: '12px',
    borderBottom: '1px solid var(--ui-stroke-tertiary)'
  },
  row: { padding: '8px 12px', borderBottom: '1px solid var(--ui-stroke-tertiary)' },
  link: {
    background: 'transparent',
    border: 'none',
    padding: '0',
    cursor: 'pointer',
    color: 'var(--ui-accent)',
    font: 'inherit'
  },
  card: {
    marginTop: '8px',
    padding: '8px',
    border: '1px solid var(--ui-stroke-tertiary)',
    borderRadius: '6px',
    fontSize: '12px',
    color: 'var(--ui-text-secondary)'
  },
  mini: {
    fontSize: '12px',
    color: 'var(--ui-text-tertiary)',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap'
  },
  th: {
    textAlign: 'left',
    padding: '6px 8px',
    borderBottom: '1px solid var(--ui-stroke-secondary)',
    color: 'var(--ui-text-tertiary)',
    fontSize: '12px',
    fontWeight: 500,
    whiteSpace: 'nowrap'
  },
  td: {
    padding: '6px 8px',
    borderBottom: '1px solid var(--ui-stroke-tertiary)',
    fontSize: '12px',
    color: 'var(--ui-text-secondary)',
    verticalAlign: 'top',
    whiteSpace: 'nowrap'
  },
  tdWrap: {
    padding: '6px 8px',
    borderBottom: '1px solid var(--ui-stroke-tertiary)',
    fontSize: '12px',
    color: 'var(--ui-text-secondary)',
    verticalAlign: 'top',
    maxWidth: '30rem',
    whiteSpace: 'normal',
    wordBreak: 'break-word'
  }
}

// ── 兜底块 ────────────────────────────────────────────────────────────────
function Loading() {
  return jsx('div', { style: { padding: '16px', color: 'var(--ui-text-tertiary)' }, children: '加载中…' })
}

function EmptyText({ text }) {
  return jsx('div', { style: { padding: '16px', color: 'var(--ui-text-tertiary)' }, children: text })
}

function ErrorBox({ error }) {
  return jsxs('div', { style: { padding: '16px' }, children: [
    jsx('div', { style: { color: 'var(--ui-danger)' }, children: '接口不可用:' + errMsg(error) }),
    jsx('div', { style: { marginTop: '4px', color: 'var(--ui-text-tertiary)' }, children: '检查后端开关(plugins.enabled)与 gateway 是否已重启' })
  ]})
}

// 统一的「加载中 / 出错 / 空 / 正常」四态。children 是 (data) => 节点。
function QueryBody({ q, isEmpty, emptyText, children }) {
  if (q.error && !q.data) return jsx(ErrorBox, { error: q.error })
  if (q.isPending || !q.data) return jsx(Loading, {})
  if (isEmpty(q.data)) return jsx(EmptyText, { text: emptyText })
  return children(q.data)
}

function useApi(path) {
  return useQuery({
    queryKey: ['kanban-audit', path],
    queryFn: () => rest(path),
    refetchInterval: POLL_MS
  })
}

// ── 小部件 ────────────────────────────────────────────────────────────────
function SeverityDot({ severity }) {
  return jsx('span', { style: { color: sevColor(severity), fontSize: '10px' }, children: '●' })
}

function TypeBadge({ text }) {
  return jsx('span', {
    style: {
      padding: '1px 6px',
      borderRadius: '4px',
      border: '1px solid var(--ui-stroke-secondary)',
      color: 'var(--ui-text-secondary)',
      whiteSpace: 'nowrap'
    },
    children: text
  })
}

// 逐轮 token 迷你条形:四段按同一标尺拼一条横条,全 0 时每段保底 1 像素。
function TokenBar({ run, max }) {
  const segs = [
    ['input', 'var(--ui-accent)'],
    ['cache_read', 'var(--ui-blue)'],
    ['output', 'var(--ui-green)'],
    ['reasoning', 'var(--ui-orange)']
  ]
  const total = segs.reduce((a, s) => a + num(run[s[0]]), 0)
  const scale = max > 0 ? max : 1
  const W = 160
  const H = 8
  let x = 0
  const rects = segs.map((s) => {
    const v = num(run[s[0]])
    const w = total > 0 ? Math.max(1, (v / scale) * W) : 1
    const rect = jsx('rect', { x: x, y: 0, width: w, height: H, rx: 1, fill: s[1] })
    x += w
    return jsx('g', { key: s[0], children: rect })
  })
  return jsx('svg', {
    width: W,
    height: H,
    viewBox: '0 0 ' + W + ' ' + H,
    style: { display: 'block', flex: '0 0 auto' },
    children: rects
  })
}

// ── 卡详情(展开区,单独走一个 useQuery)────────────────────────────────────
function CardDetail({ taskId }) {
  const q = useQuery({
    queryKey: ['kanban-audit', 'card', taskId],
    queryFn: () => rest('/card/' + taskId),
    refetchInterval: POLL_MS,
    staleTime: Infinity,
    gcTime: Infinity,
    refetchOnMount: false,
    enabled: Boolean(taskId)
  })

  if (q.error && !q.data) {
    return jsx('div', { style: S.card, children: jsx('span', { style: { color: 'var(--ui-danger)' }, children: '卡详情加载失败:' + errMsg(q.error) }) })
  }
  if (q.isPending || !q.data) return jsx('div', { style: S.card, children: '卡详情加载中…' })

  const d = q.data
  const task = d.task || {}
  const runs = arr(d.runs)
  const issues = arr(d.issues)
  const timeline = arr(d.timeline)
  const max = runs.reduce((m, r) => Math.max(m, num(r.input) + num(r.cache_read) + num(r.output) + num(r.reasoning)), 0)

  const head = jsxs('div', { style: { display: 'flex', gap: '8px', flexWrap: 'wrap', alignItems: 'center' }, children: [
    jsx('span', { style: { color: 'var(--ui-text-primary)' }, children: String(task.title || task.task_id || taskId) }),
    jsx('span', { style: { color: 'var(--ui-text-tertiary)' }, children: 'assignee ' + String(task.assignee || '—') }),
    jsx('span', { style: { color: 'var(--ui-text-tertiary)' }, children: 'status ' + String(task.status || '—') })
  ]})

  const attribution = d.attribution
    ? jsx('div', { style: { marginTop: '4px', color: 'var(--ui-text-tertiary)' }, children: '归因:' + String(d.attribution) })
    : null

  const timelineBlock = jsxs('div', { style: { marginTop: '8px' }, children: [
    jsx('div', { style: { color: 'var(--ui-text-tertiary)' }, children: '时间线' }),
    timeline.length
      ? timeline.map((t, i) => jsx('div', {
          key: String(i),
          style: S.mini,
          children: fmtTs(t && t.ts) + '  ' + String((t && t.kind) || '') + '  ' + String((t && t.text) || '')
        }))
      : jsx('div', { style: S.mini, children: '无时间线' })
  ]})

  const runsBlock = jsxs('div', { style: { marginTop: '8px' }, children: [
    jsx('div', { style: { color: 'var(--ui-text-tertiary)' }, children: '逐轮 token' }),
    runs.length
      ? runs.map((r, i) => jsxs('div', {
          key: String(r && r.run_id !== undefined ? r.run_id : i),
          style: { display: 'flex', alignItems: 'center', gap: '8px', marginTop: '4px' },
          children: [
            jsx('span', { style: { color: 'var(--ui-text-secondary)', width: '3rem' }, children: '#' + String(r && r.run_id) }),
            jsx('span', { style: { width: '7rem', overflow: 'hidden', textOverflow: 'ellipsis' }, children: String((r && r.profile) || '—') }),
            jsx('span', { style: { width: '4rem' }, children: String((r && r.status) || '—') }),
            jsx('span', { style: { width: '4rem', color: 'var(--ui-text-tertiary)' }, children: secs(r && r.wall) }),
            jsx('span', { style: { width: '4rem', color: 'var(--ui-text-tertiary)' }, children: 'api ' + String(num(r && r.api_calls)) }),
            jsx(TokenBar, { run: r || {}, max: max }),
            jsx('span', { style: { color: 'var(--ui-text-tertiary)' }, children: 'cache 命中 ' + pct1(r && r.cache_reuse) })
          ]
        }))
      : jsx('div', { style: S.mini, children: '无运行记录' })
  ]})

  const issuesBlock = jsxs('div', { style: { marginTop: '8px' }, children: [
    jsx('div', { style: { color: 'var(--ui-text-tertiary)' }, children: '该卡异常' }),
    issues.length
      ? issues.map((it, i) => jsxs('div', {
          key: String((it && it.id) || i),
          style: { display: 'flex', gap: '8px', marginTop: '2px' },
          children: [
            jsx('span', { style: { color: sevColor(num(it && it.severity)) }, children: String((it && it.type) || '—') }),
            jsx('span', { style: { color: 'var(--ui-text-tertiary)' }, children: String(num(it && it.severity)) }),
            jsx('span', { children: String((it && it.suggestion) || '') })
          ]
        }))
      : jsx('div', { style: S.mini, children: '无异常' })
  ]})

  return jsxs('div', { style: S.card, children: [head, attribution, timelineBlock, runsBlock, issuesBlock] })
}

// ── 问题视图 ──────────────────────────────────────────────────────────────
function IssueRow({ issue }) {
  const [showEvidence, setShowEvidence] = useState(false)
  const [showCard, setShowCard] = useState(false)
  const evidence = arr(issue.evidence)
  const sev = num(issue.severity)

  const top = jsxs('div', { style: { display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }, children: [
    jsx(SeverityDot, { severity: sev }),
    jsx(TypeBadge, { text: String(issue.type || '—') }),
    jsx('span', { style: { color: 'var(--ui-text-tertiary)' }, children: String(sev) }),
    jsx('button', {
      type: 'button',
      onClick: () => setShowCard((v) => !v),
      title: String(issue.title || ''),
      style: S.link,
      children: String(issue.task_id || '—')
    }),
    jsx('span', { style: { flex: '1 1 8rem', minWidth: '6rem', color: 'var(--ui-text-secondary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }, children: String(issue.title || '') }),
    jsx('button', { type: 'button', onClick: () => setShowEvidence((v) => !v), style: S.link, children: showEvidence ? '收起' : '展开' })
  ]})

  const detail = showEvidence
    ? jsxs('div', { style: { marginTop: '6px', paddingLeft: '18px' }, children: [
        evidence.length
          ? jsx('ul', { style: { margin: '0', paddingLeft: '16px', color: 'var(--ui-text-secondary)' }, children: evidence.map((e, i) => jsx('li', { key: String(i), children: String(e) })) })
          : jsx('div', { style: { color: 'var(--ui-text-tertiary)' }, children: '无证据' }),
        jsx('div', { style: { marginTop: '4px', color: 'var(--ui-text-secondary)' }, children: '建议:' + String(issue.suggestion || '—') })
      ]})
    : null

  return jsxs('div', { style: S.row, children: [
    top,
    detail,
    showCard ? jsx(CardDetail, { taskId: String(issue.task_id || '') }) : null
  ]})
}

function IssuesView({ summaryQ, issuesQ }) {
  const c = (summaryQ.data && summaryQ.data.counts) || {}
  return jsxs('div', { children: [
    jsx('div', {
      style: S.counts,
      children: '运行 ' + num(c.running) + ' · 待办 ' + num(c.todo) + ' · 完成 ' + num(c.done) + ' · 阻塞 ' + num(c.blocked)
    }),
    jsx(QueryBody, {
      q: issuesQ,
      emptyText: '没有异常项',
      isEmpty: (d) => arr(d.issues).length === 0,
      children: (d) => {
        const list = arr(d.issues).slice().sort((a, b) => num(b && b.severity) - num(a && a.severity))
        return jsx('div', { children: list.map((it, i) => jsx(IssueRow, { key: String((it && it.id) || i), issue: it })) })
      }
    })
  ]})
}

// ── 专家画像视图(表头可点排序)────────────────────────────────────────────
const AGENT_COLS = [
  { key: 'profile', label: '专家' },
  { key: 'cards', label: '卡数' },
  { key: 'completion_rate', label: '完成率' },
  { key: 'wall_p50', label: '耗时 p50' },
  { key: 'wall_p95', label: '耗时 p95' },
  { key: 'cache_reuse', label: '复用度' },
  { key: 'per_call_output', label: '每轮产出' },
  { key: 'cost_usd', label: '成本' }
]

function SortTh({ col, sort, onSort }) {
  const active = sort.key === col.key
  return jsx('th', { style: S.th, children: jsx('button', {
    type: 'button',
    onClick: () => onSort(col.key),
    style: { ...S.link, color: active ? 'var(--ui-accent)' : 'inherit' },
    children: col.label + (active ? (sort.dir > 0 ? ' ▲' : ' ▼') : '')
  })})
}

function ReuseCell({ agent }) {
  const v = optNum(agent.cache_reuse)
  const base = optNum(agent.cache_baseline_min)
  if (v === null) return jsx('span', { children: '—' })
  const text = pct1(v) + '(线 ' + (base === null ? '—' : pct1(base)) + ')'
  const bad = base !== null && v < base
  return jsx('span', { style: bad ? { color: 'var(--ui-danger)' } : undefined, children: bad ? '⚠ ' + text : text })
}

function AgentsView({ q }) {
  const [sort, setSort] = useState({ key: 'profile', dir: 1 })

  const onSort = (key) => setSort((s) => (s.key === key ? { key: key, dir: -s.dir } : { key: key, dir: 1 }))

  return jsx(QueryBody, {
    q: q,
    emptyText: '暂无专家数据',
    isEmpty: (d) => arr(d.agents).length === 0,
    children: (d) => {
      const rows = arr(d.agents).slice().sort((a, b) => {
        const va = a ? a[sort.key] : undefined
        const vb = b ? b[sort.key] : undefined
        const bothNum = optNum(va) !== null && optNum(vb) !== null
        const r = bothNum
          ? optNum(va) - optNum(vb)
          : String(va === undefined || va === null ? '' : va).localeCompare(String(vb === undefined || vb === null ? '' : vb), 'zh')
        return r * sort.dir
      })
      return jsxs('table', { style: { width: '100%', borderCollapse: 'collapse' }, children: [
        jsx('thead', { children: jsx('tr', { children: AGENT_COLS.map((col) => jsx(SortTh, { key: col.key, col: col, sort: sort, onSort: onSort })) }) }),
        jsx('tbody', { children: rows.map((a, i) => jsxs('tr', {
          key: String((a && a.profile) || i),
          children: [
            jsx('td', { style: S.td, children: String((a && a.profile) || '—') }),
            jsx('td', { style: S.td, children: String(num(a && a.cards)) }),
            jsx('td', { style: S.td, children: pct1(a && a.completion_rate) }),
            jsx('td', { style: S.td, children: secs(a && a.wall_p50) }),
            jsx('td', { style: S.td, children: secs(a && a.wall_p95) }),
            jsx('td', { style: S.td, children: jsx(ReuseCell, { agent: a || {} }) }),
            jsx('td', { style: S.td, children: fix1(a && a.per_call_output) }),
            jsx('td', { style: S.td, children: money(a && a.cost_usd) })
          ]
        })) })
      ]})
    }
  })
}

// ── 缺口视图 ──────────────────────────────────────────────────────────────
const GAP_COLS = ['缺什么', '谁受影响', '影响卡数', '建议动作', '优先级']

function GapsView({ q }) {
  return jsx(QueryBody, {
    q: q,
    emptyText: '暂无缺口',
    isEmpty: (d) => arr(d.gaps).length === 0,
    children: (d) => jsxs('table', { style: { width: '100%', borderCollapse: 'collapse' }, children: [
      jsx('thead', { children: jsx('tr', { children: GAP_COLS.map((label) => jsx('th', { key: label, style: S.th, children: label })) }) }),
      jsx('tbody', { children: arr(d.gaps).map((g, i) => {
        const who = arr(g && g.who).join('、')
        return jsxs('tr', {
          key: String(i),
          children: [
            jsx('td', { style: S.tdWrap, children: String((g && g.what) || '—') }),
            jsx('td', { style: S.td, children: who || '—' }),
            jsx('td', { style: S.td, children: String(num(g && g.affected_cards)) }),
            jsx('td', { style: S.tdWrap, children: String((g && g.action) || '—') }),
            jsx('td', { style: S.td, children: String((g && g.priority) || '—') })
          ]
        })
      }) })
    ]})
  })
}

// ── 顶部状态条 + 页面 ─────────────────────────────────────────────────────
function TabButton({ id, current, onSelect, label }) {
  const active = id === current
  return jsx('button', {
    type: 'button',
    onClick: () => onSelect(id),
    style: {
      padding: '2px 10px',
      borderRadius: '6px',
      border: '1px solid ' + (active ? 'var(--ui-accent)' : 'var(--ui-stroke-tertiary)'),
      color: active ? 'var(--ui-accent)' : 'var(--ui-text-secondary)',
      background: 'transparent',
      cursor: 'pointer',
      font: 'inherit'
    },
    children: label
  })
}

function AuditPage() {
  const [tab, setTab] = useState('issues')
  const healthQ = useApi('/health')
  const summaryQ = useApi('/summary')
  const issuesQ = useApi('/issues')
  const agentsQ = useApi('/agents')
  const gapsQ = useApi('/gaps')

  const h = healthQ.data || {}
  const viewQ = tab === 'issues' ? issuesQ : tab === 'agents' ? agentsQ : gapsQ
  const genAt = h.generated_at || (viewQ.data && viewQ.data.generated_at)
  const missing = arr(h.missing)
  const rate = optNum(h.session_resolution_rate)
  const showWarn = rate !== null && rate < 1

  const header = jsxs('div', { style: S.header, children: [
    jsx('div', { style: { fontWeight: 600 }, children: 'Kanban 审计' }),
    jsxs('div', { style: { display: 'flex', gap: '6px' }, children: [
      jsx(TabButton, { id: 'issues', current: tab, onSelect: setTab, label: '问题' }),
      jsx(TabButton, { id: 'agents', current: tab, onSelect: setTab, label: '专家画像' }),
      jsx(TabButton, { id: 'gaps', current: tab, onSelect: setTab, label: '缺口' })
    ]}),
    jsxs('div', { style: { marginLeft: 'auto', display: 'flex', gap: '8px', color: 'var(--ui-text-tertiary)', fontSize: '12px' }, children: [
      jsx('span', { children: fmtTs(genAt) }),
      jsx('span', { children: '⟳ 5s 轮询' })
    ]})
  ]})

  const warn = showWarn
    ? jsxs('div', { style: S.warn, children: [
        jsx('div', { children: '数据不全:缺 ' + missing.length + ' 条 run 的会话' }),
        jsx('div', { style: { marginTop: '2px', color: 'var(--ui-text-tertiary)', fontSize: '12px' }, children: '这些 run 没有 worker_session_id,相关 token 指标不计入' })
      ]})
    : null

  return jsxs('div', { style: { display: 'flex', flexDirection: 'column', height: '100%', minHeight: '0' }, children: [
    header,
    warn,
    jsxs('div', { style: { flex: '1 1 auto', minHeight: '0', overflow: 'auto' }, children: [
      tab === 'issues' ? jsx(IssuesView, { summaryQ: summaryQ, issuesQ: issuesQ }) : null,
      tab === 'agents' ? jsx(AgentsView, { q: agentsQ }) : null,
      tab === 'gaps' ? jsx(GapsView, { q: gapsQ }) : null
    ]})
  ]})
}

export default {
  id: 'kanban-audit',
  name: 'Kanban Audit',
  register(ctx) {
    rest = (path) => ctx.rest(path)
    ctx.registerMany([
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: '/audit' },
        render: () => jsx(AuditPage, {})
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        data: { path: '/audit', label: 'Audit', codicon: 'shield' }
      }
    ])
  }
}
