import { Alert, Button, Form, Input } from 'antd'
import { Bot, MessageSquare, ShieldCheck, Sparkles, Zap } from 'lucide-react'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../store'

const FEATURES = [
  { icon: Bot, title: 'AI 自动接待', desc: 'RAG 知识库加持，7×24 秒级响应' },
  { icon: MessageSquare, title: '多平台聚合', desc: '抖音 / 小红书私信统一工作台' },
  { icon: ShieldCheck, title: '合规护栏', desc: '敏感词过滤 + 人工兜底，全程留痕' },
  { icon: Zap, title: '无缝转人工', desc: '一键接管，AI 小结上下文不丢失' },
]

export default function Login() {
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const { login } = useAuth()
  const navigate = useNavigate()

  const submit = async (values: { username: string; password: string }) => {
    setLoading(true)
    setError('')
    try {
      await login(values.username, values.password)
      navigate('/')
    } catch (err) {
      setError(err instanceof Error && err.message ? err.message : '用户名或密码错误')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="h-full flex">
      <div className="hidden lg:flex flex-1 bg-gradient-to-br from-primary-700 via-primary-600 to-teal-700 text-white flex-col justify-center px-14 relative overflow-hidden">
        <div className="absolute -top-24 -right-24 w-72 h-72 rounded-full bg-white/10" />
        <div className="absolute -bottom-32 -left-16 w-96 h-96 rounded-full bg-white/5" />
        <div className="relative">
          <div className="flex items-center gap-2.5 mb-6">
            <div className="w-10 h-10 rounded-xl bg-white/15 backdrop-blur flex items-center justify-center">
              <Sparkles size={20} />
            </div>
            <span className="text-xl font-semibold tracking-wide">智能客服</span>
          </div>
          <h1 className="text-3xl font-bold leading-snug mb-3">
            一个工作台
            <br />
            接住所有平台的咨询
          </h1>
          <p className="text-white/70 text-sm mb-10">AI 先答 · 人工兜底 · 数据可衡量</p>
          <div className="grid grid-cols-2 gap-4 max-w-md">
            {FEATURES.map((f) => (
              <div key={f.title} className="bg-white/10 backdrop-blur rounded-xl p-3.5">
                <f.icon size={18} className="mb-2 text-white/90" />
                <div className="text-sm font-medium">{f.title}</div>
                <div className="text-xs text-white/60 mt-0.5">{f.desc}</div>
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className="w-full lg:w-[440px] bg-white dark:bg-gray-900 flex items-center justify-center px-8">
        <div className="w-full max-w-xs">
          <div className="lg:hidden flex items-center gap-2 mb-6 justify-center">
            <div className="w-9 h-9 rounded-xl bg-primary-600 flex items-center justify-center text-white">
              <Sparkles size={18} />
            </div>
            <span className="text-lg font-semibold dark:text-gray-100">智能客服</span>
          </div>
          <h2 className="text-xl font-semibold text-gray-800 dark:text-gray-100 mb-1">欢迎回来</h2>
          <p className="text-xs text-gray-400 dark:text-gray-500 mb-6">登录客服工作台开始接待</p>
          <Form layout="vertical" onFinish={submit} requiredMark={false}>
            <Form.Item label="用户名" name="username" rules={[{ required: true, message: '请输入用户名' }]}>
              <Input placeholder="admin" autoFocus />
            </Form.Item>
            <Form.Item label="密码" name="password" rules={[{ required: true, message: '请输入密码' }]}>
              <Input.Password placeholder="请输入密码" />
            </Form.Item>
            {error ? <Alert type="error" message={error} showIcon className="!mb-4" /> : null}
            <Button type="primary" htmlType="submit" block loading={loading}>
              登录
            </Button>
          </Form>
        </div>
      </div>
    </div>
  )
}
