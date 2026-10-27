import { describe, expect, it } from 'vitest'
import { accountDutyRows, dedupeProfiles, fleetPresence, replyBody, rpaProfileBinding } from './accountBinding'

describe('dedupeProfiles', () => {
  it('keeps one option when several workers report the same environment', () => {
    const profiles = dedupeProfiles([
      { browser_profiles: [{ id: 'p1', name: '环境甲' }, { id: 'p2', name: '环境乙' }] },
      { browser_profiles: [{ id: 'p1', name: '环境甲' }] },
    ])
    expect(profiles).toEqual([
      { id: 'p1', name: '环境甲' },
      { id: 'p2', name: '环境乙' },
    ])
  })
})

describe('rpaProfileBinding', () => {
  it('does not send a worker id', () => {
    const payload = rpaProfileBinding({
      platform: 'douyin', duty: 'dm', profile_id: 'p1', profile_name: '环境甲',
    })
    expect(payload).toEqual({
      platform: 'douyin',
      duty: 'dm',
      adspower_profile_id: 'p1',
      profile_name: '环境甲',
    })
    expect(payload).not.toHaveProperty('worker_id')
  })
})

describe('accountDutyRows', () => {
  it('splits two duties and only enables a disabled row', () => {
    const rows = accountDutyRows(
      [{ id: 1, auth_type: 'rpa' }],
      [{
        id: 1,
        bindings: {
          dm: {
            id: 10, duty: 'dm', auth_status: 'authorized', worker_status: 'online',
            adspower_profile_id: 'p1',
          },
          comment: {
            id: 11, duty: 'comment', auth_status: 'disabled', worker_status: 'offline',
            adspower_profile_id: 'p2',
          },
        },
      }],
      [{ id: 'p1', name: '环境甲' }],
    )
    expect(rows.map((row) => row.duty)).toEqual(['dm', 'comment'])
    expect(rows[0].profileId).toBe('p1')
    expect(rows[0].profileName).toBe('环境甲')
    expect(rows[1].profileId).toBe('p2')
    expect(rows[1].profileName).toBe('')
    expect(rows[0].showDelete).toBe(true)
    expect(rows[0].canDisable).toBe(true)
    expect(rows[0].canEnable).toBe(false)
    expect(rows[1].showDelete).toBe(false)
    expect(rows[1].canEnable).toBe(true)
    expect(rows[1].canReopen).toBe(false)
  })

  it('offers reopen for the expired duty only', () => {
    const rows = accountDutyRows(
      [{ id: 2, auth_type: 'rpa' }],
      [{
        id: 2,
        profile_name: '环境丙',
        bindings: {
          dm: {
            id: 20, duty: 'dm', auth_status: 'login_expired', worker_status: 'login_expired',
            adspower_profile_id: 'p9', debug_file: 'snap.html', account_type_mismatch: true,
          },
        },
      }],
      [],
    )
    expect(rows).toHaveLength(1)
    expect(rows[0].canReopen).toBe(true)
    expect(rows[0].bindingId).toBe(20)
    expect(rows[0].profileId).toBe('p9')
    expect(rows[0].profileName).toBe('环境丙')
    expect(rows[0].debugFile).toBe('snap.html')
    expect(rows[0].accountTypeMismatch).toBe(true)
  })
})

describe('replyBody', () => {
  it('unwraps comment JSON and keeps plain dm text', () => {
    expect(replyBody('comment_reply', JSON.stringify({ text: '评论回复呀', comment_id: 'c1' }))).toBe('评论回复呀')
    expect(replyBody('first_comment', JSON.stringify({ text: '首评引流' }))).toBe('首评引流')
    expect(replyBody('text', '私信你好')).toBe('私信你好')
  })
})

describe('fleetPresence', () => {
  it('distinguishes running, offline, and missing', () => {
    expect(fleetPresence([{ worker_id: 'fleet-pc', status: 'fleet' }])).toBe('running')
    expect(fleetPresence([{ worker_id: 'fleet-pc', status: 'offline' }])).toBe('offline')
    expect(fleetPresence([{ worker_id: 'worker-1', status: 'online' }])).toBe('missing')
  })
})
