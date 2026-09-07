/**
 * The board feature's public surface.
 *
 * One screen, mounted by the route table at `/:workspaceSlug/board`. Which
 * team's board it is, and how it is filtered, sorted and grouped, all come
 * from the query string -- so there is exactly one route and no second name
 * for it.
 */

export { BoardScreen } from './pages/BoardScreen'
