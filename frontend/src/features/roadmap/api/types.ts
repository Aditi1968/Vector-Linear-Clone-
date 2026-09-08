/**
 * The names this feature knows the server contract by.
 *
 * Aliases of generated types, every one. Nothing here declares a field, a
 * scalar or a nullability, so nothing here can disagree with the schema.
 */

import type {
  RoadmapInitiativeFieldsFragment,
  RoadmapProjectFieldsFragment,
} from '../../../generated/operations'

export type { Health, InitiativeStatus, ProjectState } from '../../../generated/schema'

/** One initiative, with only the fields a calendar needs. */
export type RoadmapInitiative = RoadmapInitiativeFieldsFragment

/** One project, with only the fields a calendar needs. */
export type RoadmapProject = RoadmapProjectFieldsFragment
