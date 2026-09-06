import { describe, expect, it } from 'vitest'

import {
  isValidSlug,
  isValidTeamKey,
  proposeTeamKey,
  slugify,
} from './identifiers'

/**
 * The proposals, at the edges where they would otherwise propose something
 * the server refuses.
 *
 * Every case below is one the happy path never reaches and a person hits on
 * their first afternoon: a company name with a comma in it, an accent, a
 * number at the front. A proposal that arrives pre-rejected is worse than no
 * proposal, because it looks like the form filled itself in correctly.
 */
describe('slugify', () => {
  it('proposes something the slug rule accepts', () => {
    for (const name of [
      'Acme',
      'Acme, Inc.',
      '  Acme   Corp  ',
      'Café Ø',
      'Acme -- 2026!',
      'ACME',
    ]) {
      const slug = slugify(name)

      expect(isValidSlug(slug), `${name} -> ${slug}`).toBe(true)
    }
  })

  it('decomposes accents rather than dropping the letter', () => {
    // NFKD splits the accented letter into a plain one plus a combining mark;
    // without it "Café" loses the e entirely and becomes "caf".
    expect(slugify('Café')).toBe('cafe')
  })

  it('yields an empty proposal it cannot make, rather than a broken one', () => {
    // No transliteration for non-Latin scripts, on purpose -- that is a
    // library. The caller treats '' as "no proposal" and leaves the field to
    // the person.
    expect(slugify('...')).toBe('')
    expect(slugify('')).toBe('')
  })
})

describe('proposeTeamKey', () => {
  it('proposes something the key rule accepts', () => {
    for (const name of [
      'Engineering',
      'Design Systems',
      '3M Platform',
      'Growth & Retention',
      'a',
    ]) {
      const key = proposeTeamKey(name)

      expect(isValidTeamKey(key), `${name} -> ${key}`).toBe(true)
    }
  })

  it('initials a multi-word name and truncates a single word', () => {
    expect(proposeTeamKey('Design Systems')).toBe('DS')
    expect(proposeTeamKey('Engineering')).toBe('ENG')
  })

  it('drops a leading digit, which a key may not start with', () => {
    expect(proposeTeamKey('24/7 Support')).toBe('S')
  })

  it('proposes nothing rather than something invalid', () => {
    expect(proposeTeamKey('')).toBe('')
    expect(proposeTeamKey('42')).toBe('')
  })
})
