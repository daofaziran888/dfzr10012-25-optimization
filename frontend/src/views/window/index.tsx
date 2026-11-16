import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/window'
const columns = [
  "窗口编号",
  "数据流名称",
  "窗口类型",
  "窗口长度",
  "滑动步长",
  "延迟容忍",
  "触发策略",
  "窗口状态"
]
const actions = [
  "启用窗口",
  "暂停窗口",
  "关闭窗口"
]
const statuses = [
  "草稿",
  "生效中",
  "已暂停",
  "已关闭"
]
const stats = [
  {
    "label": "窗口定义总数",
    "value": 0
  },
  {
    "label": "生效中窗口",
    "value": 0
  },
  {
    "label": "已暂停窗口",
    "value": 0
  }
]

type Row = Record<string, string | number | null>
type RecalcResult = Record<string, string | number | null>

export default function WindowView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [recalc, setRecalc] = useState<RecalcResult | null>(null)
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('窗口定义列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '窗口计算配置列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runRecalc(row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/recalculations`, {
        method: 'POST',
        body: JSON.stringify({ values: {} }),
      })
      const payload = await response.json()
      if (!payload.ok) {
        throw new Error(payload.message ?? '窗口重算未完成')
      }
      setNotice(payload.message ?? '窗口重算已完成')
      setRecalc(payload.entry as RecalcResult)
      await reload()
    } catch (error) {
      setRecalc(null)
      setErrorMessage(error instanceof Error ? error.message : '窗口重算失败')
    }
  }

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      if (!response.ok) {
        throw new Error('窗口计算配置动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '窗口计算配置操作失败')
    }
  }

  return (
    <section className="page" data-module="window">
      <header className="page-head">
        <div>
          <h2>窗口计算配置管理</h2>
          <p className="page-desc">维护窗口计算的类型选择、长度配置与触发策略，为窗口计算配置提供可验证的数据工程流程。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('窗口定义登记入口尚未接入审批流')}>
            登记窗口定义
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出窗口计算配置清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
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
            <th>重算水位线版本</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td>{row.重算水位线版本 ?? '—'}</td>
              <td className="row-actions">
                <button className="link" type="button" onClick={() => void runRecalc(row)}>
                  按水位线快照重算
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
              <td className="empty-state" colSpan={columns.length + 2}>暂无窗口计算配置数据，可先登记窗口定义</td>
            </tr>
          )}
        </tbody>
      </table>

      {recalc && (
        <section className="stat-card" style={{ marginTop: 12 }}>
          <h3>最近一次窗口重算 · {String(recalc.窗口编号 ?? '')}</h3>
          <p className="page-desc">
            水位线版本 {String(recalc.生效版本 ?? '—')} · {String(recalc.水位线版本ID ?? '—')} ·
            延迟容忍 {String(recalc.延迟容忍 ?? '—')} · 空闲超时 {String(recalc.空闲超时 ?? '—')} ·
            快照来源 {String(recalc.快照来源 ?? '—')}
          </p>
        </section>
      )}

      <footer className="page-foot">
        <span>共 {total} 条窗口计算配置记录</span>
        {notice && <span>{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
