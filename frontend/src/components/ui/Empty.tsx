/** 空状态：antd Empty */
import { Empty as AntEmpty } from 'antd'
import { LucideIcon } from 'lucide-react'

interface Props {
  icon?: LucideIcon
  title: string
  hint?: string
}

export default function Empty({ icon: Icon, title, hint }: Props) {
  return (
    <AntEmpty
      image={Icon ? <Icon size={40} strokeWidth={1.25} /> : AntEmpty.PRESENTED_IMAGE_SIMPLE}
      description={
        <div>
          <div>{title}</div>
          {hint ? <div style={{ marginTop: 4, fontSize: 12, opacity: 0.65 }}>{hint}</div> : null}
        </div>
      }
    />
  )
}
