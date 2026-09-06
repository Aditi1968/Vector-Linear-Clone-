import { describe, expect, it } from 'vitest'

import { ROUTE_SEGMENTS, createAppPaths, paths } from './paths'

/**
 * The path builders.
 *
 * Two things are worth pinning here and nothing else is. The rest of routing
 * is asserted by the shell's tests, which navigate.
 */
describe('paths', () => {
  /**
   * The bound set is produced from the slug-first set by a cast that
   * TypeScript cannot check (`Object.fromEntries` returns `any`-shaped
   * entries). This is what checks it: every builder, called both ways, with
   * enough arguments for the widest of them.
   */
  it('binds every builder to the same URL the slug-first form produces', () => {
    const bound = createAppPaths('acme')
    const slugFirst: Record<string, (...args: string[]) => string> = paths
    const applied: Record<string, (...args: string[]) => string> = bound
    const names = Object.keys(slugFirst)

    expect(names.length).toBeGreaterThan(0)

    for (const name of names) {
      const build = slugFirst[name]
      const boundBuild = applied[name]

      expect(build).toBeDefined()
      expect(boundBuild).toBeDefined()

      // 'ENG' stands in for both a team key and an id; no builder takes more
      // than one argument after the slug.
      expect(boundBuild?.('ENG')).toBe(build?.('acme', 'ENG'))
    }
  })

  /**
   * A slug or an id becomes a path segment, and neither is under the router's
   * control -- a workspace slug is chosen by a user and an issue id comes
   * from the server. Encoding is what stops one of them ending a segment
   * early.
   */
  it('encodes every value it is given', () => {
    expect(paths.issue('a/b', 'c d')).toBe('/a%2Fb/issues/c%20d')
    expect(paths.teamIssues('acme', '../etc')).toBe('/acme/team/..%2Fetc/issues')
  })

  /**
   * The router declares children as relative segments and the builders write
   * absolute URLs, so the two could disagree without either being obviously
   * wrong. This is the one pairing where that would be silent: a link that
   * renders, matches no route, and lands on the shell's not-found page.
   */
  it('builds URLs the route table declares', () => {
    expect(paths.myIssues('acme')).toBe(`/acme/${ROUTE_SEGMENTS.myIssues}`)
    expect(paths.inbox('acme')).toBe(`/acme/${ROUTE_SEGMENTS.inbox}`)
    expect(paths.search('acme')).toBe(`/acme/${ROUTE_SEGMENTS.search}`)
    expect(paths.teamIssues('acme', 'ENG')).toBe(
      `/acme/${ROUTE_SEGMENTS.teamIssues.replace(':teamKey', 'ENG')}`,
    )
    expect(paths.cycles('acme', 'ENG')).toBe(
      `/acme/${ROUTE_SEGMENTS.cycles.replace(':teamKey', 'ENG')}`,
    )
  })
})
