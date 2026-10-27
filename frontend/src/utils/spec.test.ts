import { describe, expect, it } from 'vitest'
import { countTags, isOverLimit } from './spec'

describe('countTags', () => {
  it('splits on chinese and english commas and ignores blanks', () => {
    expect(countTags('护肤,补水，干皮,  ')).toBe(3)
    expect(countTags('')).toBe(0)
    expect(countTags('  ,  ， ')).toBe(0)
  })

  it('counts a single tag without comma', () => {
    expect(countTags('护肤')).toBe(1)
  })
})

describe('isOverLimit', () => {
  it('is over when current exceeds max', () => {
    expect(isOverLimit(21, 20)).toBe(true)
    expect(isOverLimit(20, 20)).toBe(false)
  })

  it('never over when max is missing or zero', () => {
    expect(isOverLimit(999)).toBe(false)
    expect(isOverLimit(999, 0)).toBe(false)
  })
})
