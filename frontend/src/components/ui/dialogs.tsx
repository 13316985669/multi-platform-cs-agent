/** Promise 式对话框：confirmDialog / promptDialog，替代 window.confirm / window.prompt */
import { Input, Modal } from 'antd'
import type { InputRef } from 'antd'
import { FormEvent, useEffect, useRef, useState } from 'react'
import { create } from 'zustand'

interface ConfirmOptions {
  title: string
  message?: string
  confirmText?: string
  danger?: boolean
}

interface PromptOptions {
  title: string
  placeholder?: string
  defaultValue?: string
  confirmText?: string
}

type DialogReq =
  | ({ kind: 'confirm'; resolve: (v: boolean) => void } & ConfirmOptions)
  | ({ kind: 'prompt'; resolve: (v: string | null) => void } & PromptOptions)

interface DialogState {
  current: DialogReq | null
  open: (req: DialogReq) => void
  close: () => void
}

const useDialogStore = create<DialogState>((set) => ({
  current: null,
  open: (req) => set({ current: req }),
  close: () => set({ current: null }),
}))

/** 确认对话框：返回 Promise<boolean>（danger=true 时确认键为红色） */
export function confirmDialog(opts: ConfirmOptions): Promise<boolean> {
  return new Promise((resolve) => useDialogStore.getState().open({ kind: 'confirm', ...opts, resolve }))
}

/** 输入对话框：返回 Promise<string | null>（取消为 null） */
export function promptDialog(opts: PromptOptions): Promise<string | null> {
  return new Promise((resolve) => useDialogStore.getState().open({ kind: 'prompt', ...opts, resolve }))
}

export function DialogHost() {
  const current = useDialogStore((s) => s.current)
  const close = useDialogStore((s) => s.close)
  const [value, setValue] = useState('')
  const inputRef = useRef<InputRef>(null)

  useEffect(() => {
    if (current?.kind === 'prompt') {
      setValue(current.defaultValue || '')
      setTimeout(() => inputRef.current?.focus(), 50)
    }
  }, [current])

  if (!current) return null

  const done = (v: boolean | string | null) => {
    current.resolve(v as never)
    close()
  }

  const submit = (e?: FormEvent) => {
    e?.preventDefault()
    done(current.kind === 'prompt' ? value : true)
  }

  return (
    <Modal
      open
      title={current.title}
      okText={current.confirmText || '确认'}
      cancelText="取消"
      okButtonProps={{ danger: current.kind === 'confirm' && current.danger }}
      onOk={() => submit()}
      onCancel={() => done(current.kind === 'prompt' ? null : false)}
      destroyOnClose
    >
      {current.kind === 'confirm' && current.message && <div>{current.message}</div>}
      {current.kind === 'prompt' && (
        <Input
          ref={inputRef}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder={current.placeholder}
          onPressEnter={() => submit()}
        />
      )}
    </Modal>
  )
}
