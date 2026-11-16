import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/watermark'
const columns = [
  "配置编号",
  "流名称",
  "时间字段",
  "延迟容忍",
  "空闲超时",
  "发射间隔",
  "对齐策略",
  "配置状态"
]
const actions = [
  "启用水位线",
  "暂停水位线",
  "关闭水位线"
]
const statuses = [
  "草稿",
  "已启用",
  "已暂停",
  "已关闭"
]
const stats = [
  {
    "label": "水位线配置总数",
    "value": 0
  },
  {
    "label": "已启用配置",
    "value": 0
  },
  {
    "label": "已暂停配置",
    "value": 0
  }
]

type Row = Record<string, string | number | null>
type Version = Record<string, string | number | null>
type Consumer = Record<string, string | number | null>

type Detail = Record<string, unknown> & {
  当前生效版本?: Version | null
  版本列表?: Version[]
  消费记录?: Consumer[]
  版本一致性?: { ok: boolean; 生效版本: string | null; 水位线版本ID: string | null }
}

export default function WatermarkView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [detail, setDetail] = useState<Detail | null>(null)
  const [detailId, setDetailId] = useState<number | null>(null)
  const [draftTolerance, setDraftTolerance] = useState('')
  const [draftIdle, setDraftIdle] = useState('')
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('水位线配置列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '水位线管理列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  const openDetail = useCallback(async (rowId: number) => {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${rowId}`)
      if (!response.ok) {
        throw new Error('水位线配置详情读取失败')
      }
      const payload = (await response.json()) as Detail
      setDetail(payload)
      setDetailId(rowId)
      setDraftTolerance(String(payload['延迟容忍'] ?? payload.当前生效版本?.延迟容忍 ?? ''))
      setDraftIdle(String(payload['空闲超时'] ?? payload.当前生效版本?.空闲超时 ?? ''))
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '水位线配置详情读取失败')
    }
  }, [])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      if (!response.ok) {
        throw new Error('水位线管理动作未生效，请稍后重试')
      }
      await reload()
      if (detailId === Number(row.id)) {
        await openDetail(Number(row.id))
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '水位线管理操作失败')
    }
  }

  async function publishVersion() {
    if (detailId === null || !detail) {
      return
    }
    setErrorMessage('')
    setNotice('')
    const expected = String(detail.当前生效版本?.生效版本 ?? 'v0')
    try {
      const response = await request(`${ENDPOINT}/${detailId}/versions`, {
        method: 'POST',
        body: JSON.stringify({
          values: {
            expected_version: expected,
            '延迟容忍': draftTolerance,
            '空闲超时': draftIdle,
          },
        }),
      })
      const payload = await response.json()
      if (!payload.ok) {
        throw new Error(payload.message ?? '水位线配置版本写入失败')
      }
      setNotice(payload.message ?? '水位线配置版本已写入')
      await reload()
      await openDetail(detailId)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '水位线配置版本写入失败')
    }
  }

  return (
    <section className="page" data-module="watermark">
      <header className="page-head">
        <div>
          <h2>水位线管理管理</h2>
          <p className="page-desc">维护水位线的延迟容忍、空闲超时与发射策略；详情与窗口重算读取同一条已落库版本快照。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('水位线配置登记入口尚未接入审批流')}>
            登记水位线配置
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出水位线管理清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">
              {item.label === '水位线配置总数'
                ? total
                : item.label === '已启用配置'
                  ? rows.filter((row) => row.status === '已启用').length
                  : rows.filter((row) => row.status === '已暂停').length}
            </strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>生效版本</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td>{row.生效版本 ?? '—'}</td>
              <td className="row-actions">
                <button className="link" type="button" onClick={() => void openDetail(Number(row.id))}>
                  查看详情
                </button>
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 2}>暂无水位线管理数据，可先登记水位线配置</td>
            </tr>
          )}
        </tbody>
      </table>

      {detail && (
        <section className="stat-card" style={{ marginTop: 12 }}>
          <h3>配置详情 · {String(detail['配置编号'] ?? '')}</h3>
          <p className="page-desc">
            生效版本 {String(detail['生效版本'] ?? '—')} · 水位线版本ID{' '}
            {String(detail.当前生效版本?.版本ID ?? '—')} · 版本一致性{' '}
            {detail.版本一致性?.ok ? '一致' : '待校验'}
          </p>
          <div className="filter-bar">
            <label className="filter-item">
              <span>延迟容忍</span>
              <input value={draftTolerance} onChange={(event) => setDraftTolerance(event.target.value)} />
            </label>
            <label className="filter-item">
              <span>空闲超时</span>
              <input value={draftIdle} onChange={(event) => setDraftIdle(event.target.value)} />
            </label>
            <button className="btn primary" type="button" onClick={() => void publishVersion()}>
              写入配置版本
            </button>
          </div>

          <table className="data-table">
            <thead>
              <tr>
                <th>版本ID</th><th>生效版本</th><th>状态</th><th>时间字段</th>
                <th>延迟容忍</th><th>空闲超时</th><th>窗口延迟容忍</th><th>写入时间</th>
              </tr>
            </thead>
            <tbody>
              {detail.版本列表?.map((version) => (
                <tr key={String(version.版本ID)}>
                  <td>{version.版本ID}</td>
                  <td>{version.生效版本}</td>
                  <td>{version.status}</td>
                  <td>{version.时间字段}</td>
                  <td>{version.延迟容忍}</td>
                  <td>{version.空闲超时}</td>
                  <td>{version.窗口延迟容忍}</td>
                  <td>{version.写入时间}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <p className="page-desc">消费侧处理记录：{detail.消费记录?.length ?? 0} 条</p>
        </section>
      )}

      <footer className="page-foot">
        <span>共 {total} 条水位线管理记录</span>
        {notice && <span>{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
