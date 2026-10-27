/** 全局提示：toast.success/error/info 走 antd message */
import { App } from 'antd'
import { useEffect } from 'react'
import type { MessageInstance } from 'antd/es/message/interface'

type Kind = 'success' | 'error' | 'info'

let messageApi: MessageInstance | null = null

export const toast = {
  success: (text: string) => messageApi?.success(text),
  error: (text: string) => messageApi?.error(text),
  info: (text: string) => messageApi?.info(text),
}

export function Toaster() {
  const { message } = App.useApp()
  useEffect(() => {
    messageApi = message
  }, [message])
  return null
}

export type { Kind }
