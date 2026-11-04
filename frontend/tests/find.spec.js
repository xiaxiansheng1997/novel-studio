/**
 * 章内查找的纯函数：命中位置与环形步进。
 * 这两个算错最隐蔽——不报错，只是圈选「差一点点」，所以单独测。
 */
import { describe, expect, it } from 'vitest'
import { collectHits, stepIndex } from '../src/utils/find'

describe('collectHits', () => {
  it('按升序给出所有出现', () => {
    expect(collectHits('banana band', 'ban')).toEqual([[0, 3], [7, 10]])
  })

  it('忽略大小写但位置对着原文', () => {
    const hits = collectHits('DeepSeek deepseek', 'deep')
    expect(hits).toEqual([[0, 4], [9, 13]])
  })

  it('中文原样匹配', () => {
    expect(collectHits('第一段\n\n第二段里有一段', '一段')).toEqual([[1, 3], [10, 12]])
  })

  it('相邻命中不重叠：aaa 里找 aa 只给两处是错的，应给 [0,1]… 逐格推进', () => {
    // indexOf 从上次「末尾」继续：aaa → [0,2]，中间那格已被圈走，不再返回 [1,3]
    expect(collectHits('aaa', 'aa')).toEqual([[0, 2]])
  })

  it('空 needle / 空正文不给命中', () => {
    expect(collectHits('有字', '')).toEqual([])
    expect(collectHits('', 'x')).toEqual([])
    expect(collectHits(null, 'x')).toEqual([])
  })

  it('找不到给空数组', () => {
    expect(collectHits('正文', '查无此词')).toEqual([])
  })
})

describe('stepIndex', () => {
  it('两端环形回绕', () => {
    expect(stepIndex(0, -1, 3)).toBe(2)
    expect(stepIndex(2, 1, 3)).toBe(0)
    expect(stepIndex(1, 1, 3)).toBe(2)
  })

  it('没有命中不越界', () => {
    expect(stepIndex(0, 1, 0)).toBe(0)
  })

  it('delta 绝对值大于一圈也落回范围', () => {
    expect(stepIndex(0, 7, 3)).toBe(1)
    expect(stepIndex(0, -7, 3)).toBe(2)
  })
})
