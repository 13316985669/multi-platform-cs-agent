/** 骨架屏：列表加载占位 */
import { Skeleton as AntSkeleton } from 'antd'

interface Props {
  rows?: number
  className?: string
}

export default function Skeleton({ rows = 3, className = '' }: Props) {
  return (
    <div className={className}>
      {Array.from({ length: Math.max(1, Math.ceil(rows / 2)) }).map((_, i) => (
        <AntSkeleton key={i} active avatar paragraph={{ rows: 2 }} />
      ))}
    </div>
  )
}
