import { useEffect, useState } from 'react'
import { Alert, Button, Card, Form, Input, Space, Switch, Tag } from 'antd'
import { api, AutoSwitches } from '../../api/client'
import { confirmDialog } from '../../components/ui/dialogs'
import { toast } from '../../components/ui/toast'
import { useAuth } from '../../store'

export default function AutomationSettings() {
  const { agent: me } = useAuth()
  const [aiEnabled, setAiEnabled] = useState(true)
  const [autoSwitches, setAutoSwitches] = useState<AutoSwitches | null>(null)
  const [brandStyle, setBrandStyle] = useState('')

  const load = async () => {
    try {
      setAiEnabled((await api.getAiSwitch()).enabled)
    } catch { /* 忽略 */ }
    try {
      setAutoSwitches(await api.getAutoSwitches())
    } catch { /* 忽略 */ }
    try {
      setBrandStyle((await api.getBrandStyle()).brand_style_guide)
    } catch { /* 忽略 */ }
  }

  useEffect(() => {
    load()
  }, [])

  const toggleAi = async () => {
    const next = !aiEnabled
    if (!next) {
      const ok = await confirmDialog({
        title: '关闭 AI 总开关？',
        message: '关闭后所有新消息将直接转人工，AI 不再自动回复。用于 AI 失控时紧急止血。',
        confirmText: '确认关闭',
        danger: true,
      })
      if (!ok) return
    }
    const res = await api.setAiSwitch(next)
    setAiEnabled(res.enabled)
    toast[next ? 'success' : 'info'](next ? 'AI 总开关已开启' : 'AI 总开关已关闭，全部转人工')
  }

  const toggleAutoSwitch = async (key: keyof AutoSwitches) => {
    if (!autoSwitches) return
    const next = !autoSwitches[key]
    if (!next) {
      const label = key === 'comment_auto_reply_enabled' ? '评论自动回复' : '自动发布'
      const ok = await confirmDialog({
        title: `关闭${label}？`,
        message: '关闭后相关自动化动作全部停止，仅保留人工操作。用于风控/异常时紧急止血。',
        confirmText: '确认关闭',
        danger: true,
      })
      if (!ok) return
    }
    try {
      setAutoSwitches(await api.setAutoSwitches({ [key]: next }))
      toast.success(next ? '已开启' : '已关闭（仅人工）')
    } catch (e: any) {
      toast.error(e.message || '切换失败')
    }
  }

  const saveBrand = async () => {
    try {
      const res = await api.setBrandStyle(brandStyle)
      setBrandStyle(res.brand_style_guide)
      toast.success('品牌语气已保存（立即生效）')
    } catch (e: any) {
      toast.error(e.message || '保存失败')
    }
  }

  return (
    <div className="max-w-5xl">
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        {!aiEnabled && (
          <Alert
            type="error"
            message="AI 总开关已关闭：所有新消息正在直接转人工，AI 不会自动回复。"
          />
        )}

        <Card>
          <div className="flex items-center justify-between gap-4">
            <div>
              <div className="text-sm font-medium text-gray-700 dark:text-gray-200">AI 总开关（全局熔断）</div>
              <div className="text-xs text-gray-400 mt-1">
                AI 失控时一键关闭，所有会话立即转人工止血。开关会写入数据库，重启后仍然保持。
              </div>
            </div>
            <Space>
              <Tag color={aiEnabled ? 'success' : 'error'}>{aiEnabled ? 'AI 接待中' : '已关闭'}</Tag>
              <Switch
                checked={aiEnabled}
                disabled={me?.role !== 'admin'}
                onChange={() => { void toggleAi() }}
              />
            </Space>
          </div>
        </Card>

        <Card title="自动化开关（内容矩阵）">
          <div className="text-xs text-gray-400 mb-4">
            关闭后评论自动回复 / 定时发布全部停止，仅人工可操作。开关会写入数据库，重启后仍然保持。
          </div>
          <div className="flex gap-4">
            {([
              { key: 'comment_auto_reply_enabled' as const, label: '评论自动回复' },
              { key: 'publish_auto_enabled' as const, label: '自动发布' },
            ]).map((item) => (
              <Card key={item.key} size="small" style={{ flex: 1 }}>
                <div className="flex items-center justify-between gap-3">
                  <span className="text-sm text-gray-600 dark:text-gray-300">{item.label}</span>
                  <Space>
                    <Tag color={autoSwitches?.[item.key] ? 'success' : 'default'}>
                      {autoSwitches?.[item.key] ? '已开启' : '已关闭'}
                    </Tag>
                    <Switch
                      checked={!!autoSwitches?.[item.key]}
                      disabled={me?.role !== 'admin' || !autoSwitches}
                      onChange={() => { void toggleAutoSwitch(item.key) }}
                    />
                  </Space>
                </div>
              </Card>
            ))}
          </div>
        </Card>

        <Card title="品牌语气（内容创作）">
          <Form layout="vertical" requiredMark={false} onFinish={() => { void saveBrand() }}>
            <div className="text-xs text-gray-400 mb-3">
              配置后自动注入 AI 创作提示词，让全矩阵内容统一品牌调性。重启后回退到 backend/.env 的 BRAND_STYLE_GUIDE。
            </div>
            <Form.Item>
              <Input.TextArea
                value={brandStyle}
                onChange={(e) => setBrandStyle(e.target.value)}
                rows={4}
                placeholder={'如：我们是面向 25-35 岁都市女性的轻护肤品牌，语气温柔专业，自称「小编」，不用网络烂梗，强调成分与实证，结尾常用「评论区见～」'}
                disabled={me?.role !== 'admin'}
              />
            </Form.Item>
            <div className="flex justify-end">
              <Button type="primary" htmlType="submit" disabled={me?.role !== 'admin'}>
                保存品牌语气
              </Button>
            </div>
          </Form>
        </Card>
      </Space>
    </div>
  )
}
