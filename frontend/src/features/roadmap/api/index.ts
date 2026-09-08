/**
 * The roadmap data adapter.
 *
 * The boundary the rest of the feature is written against. There are no
 * mutations here: nothing on the roadmap is edited from the roadmap. A date
 * is changed on the initiative or the project it belongs to, which is where
 * the rest of that thing's fields are.
 */

export { useRoadmap } from './queries'
export type { UseRoadmapResult } from './queries'

export {
  RoadmapInitiativesDocument,
  RoadmapProjectsDocument,
} from './documents'

export type {
  Health,
  InitiativeStatus,
  ProjectState,
  RoadmapInitiative,
  RoadmapProject,
} from './types'
