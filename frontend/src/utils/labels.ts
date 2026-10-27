/** 展示标签工具：平台名 / 一稿多版变体标识 / 发布弹窗版本选项（纯函数，便于测试） */

export const PLATFORM_LABEL: Record<string, string> = {
  douyin: '抖音',
  xiaohongshu: '小红书',
}

/** 变体风格短标签（与后端 VARIANT_STYLES 顺序一致） */
export const VARIANT_STYLE_SHORT = ['测评风', '提问风', '清单风']

/** 变体标识：V1·测评风 / V2·提问风 / V3·清单风（超出 3 个按序循环） */
export function variantLabel(variantNo: number | undefined | null): string {
  const n = variantNo || 1
  return `V${n}·${VARIANT_STYLE_SHORT[(n - 1) % VARIANT_STYLE_SHORT.length]}`
}

/** 发布弹窗版本选项：选题 · 版本标题 · 平台 · 变体 */
export function publishableLabel(v: {
  item_title: string
  title: string
  platform: string
  variant_no: number
}): string {
  const platform = PLATFORM_LABEL[v.platform] || v.platform
  return `${v.item_title} · ${v.title || '（无标题）'} · ${platform} · ${variantLabel(v.variant_no)}`
}
