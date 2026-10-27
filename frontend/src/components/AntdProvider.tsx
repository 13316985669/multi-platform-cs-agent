import { App as AntdApp, ConfigProvider, theme } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { ReactNode } from 'react'
import { useTheme } from '../hooks/useTheme'

export default function AntdProvider({ children }: { children: ReactNode }) {
  const mode = useTheme((s) => s.theme)
  return (
    <ConfigProvider
      locale={zhCN}
      button={{ autoInsertSpace: false }}
      theme={{
        algorithm: mode === 'dark' ? theme.darkAlgorithm : theme.defaultAlgorithm,
        token: { colorPrimary: '#00a396', borderRadius: 8 },
      }}
    >
      <AntdApp style={{ height: '100%' }}>{children}</AntdApp>
    </ConfigProvider>
  )
}
