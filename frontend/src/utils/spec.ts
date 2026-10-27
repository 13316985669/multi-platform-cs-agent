/** 平台规格工具：话题标签计数 / 超限判定（纯函数，便于测试） */

/** 话题标签计数：中英文逗号分隔，忽略空白项 */
export function countTags(tags: string): number {
  return tags.split(/[,，]/).map((s) => s.trim()).filter(Boolean).length
}

/** 规格超限判定（max 缺省 = 无限制，不超限） */
export function isOverLimit(current: number, max?: number): boolean {
  return !!max && current > max
}
