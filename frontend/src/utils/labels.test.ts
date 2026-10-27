import { describe, expect, it } from 'vitest'
import { PLATFORM_LABEL, publishableLabel, variantLabel } from './labels'

describe('variantLabel', () => {
  it('maps 1/2/3 to 测评风/提问风/清单风', () => {
    expect(variantLabel(1)).toBe('V1·测评风')
    expect(variantLabel(2)).toBe('V2·提问风')
    expect(variantLabel(3)).toBe('V3·清单风')
  })

  it('defaults missing/zero to V1', () => {
    expect(variantLabel(undefined)).toBe('V1·测评风')
    expect(variantLabel(null)).toBe('V1·测评风')
    expect(variantLabel(0)).toBe('V1·测评风')
  })

  it('cycles style when variant exceeds 3', () => {
    expect(variantLabel(4)).toBe('V4·测评风')
    expect(variantLabel(5)).toBe('V5·提问风')
  })
})

describe('publishableLabel', () => {
  it('joins topic, title, platform, and variant', () => {
    expect(publishableLabel({
      item_title: '秋冬补水',
      title: '3 步搞定干皮',
      platform: 'xiaohongshu',
      variant_no: 2,
    })).toBe(`秋冬补水 · 3 步搞定干皮 · ${PLATFORM_LABEL.xiaohongshu} · V2·提问风`)
  })

  it('falls back when title empty and platform unknown', () => {
    expect(publishableLabel({
      item_title: '选题',
      title: '',
      platform: 'kuaishou',
      variant_no: 1,
    })).toBe('选题 · （无标题） · kuaishou · V1·测评风')
  })
})
