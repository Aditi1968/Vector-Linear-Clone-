import { useCallback, useState } from 'react'
import { useMutation, useQuery } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../app/routes'
import { describeError } from '../screens'
import {
  WorkspaceGithubAutomationSetDocument,
  WorkspaceGithubDisconnectDocument,
  WorkspaceGithubRepositoriesSetDocument,
  WorkspaceIntegrationsDocument,
  WorkspaceSlackDisconnectDocument,
} from '../../generated/operations'
import type {
  GithubIntegrationFieldsFragment,
  SlackIntegrationFieldsFragment,
  WorkspaceIntegrationsQuery,
} from '../../generated/operations'

/**
 * The settings screen's data adapter.
 *
 * The boundary the screen is written against: it imports the hook and types
 * below and never a document, an Apollo hook, or an Apollo error type. The
 * documents live in ./operations.graphql and are re-exported here only
 * because mocking a response in a test requires the exact document that
 * produced it.
 */

export {
  WorkspaceGithubAutomationSetDocument,
  WorkspaceGithubDisconnectDocument,
  WorkspaceGithubRepositoriesSetDocument,
  WorkspaceIntegrationsDocument,
  WorkspaceSlackDisconnectDocument,
} from '../../generated/operations'

export type GithubIntegration = GithubIntegrationFieldsFragment
export type SlackIntegration = SlackIntegrationFieldsFragment

/** One team and its board, which is what the automation controls offer. */
export type SettingsTeam = WorkspaceIntegrationsQuery['teams'][number]
export type GithubAutomation = GithubIntegration['automations'][number]

/**
 * The states both providers report, taken from the wider of the two enums.
 *
 * `GithubIntegrationStatus` and `SlackIntegrationStatus` are separate enums in
 * the schema, so this is the shape both narrow to rather than a claim that
 * they are the same type. GitHub's is the wider one: it alone has `PENDING`,
 * and Slack's three members are assignable to these four.
 *
 * Two of them are easy to misread and both matter:
 *
 * - `UNCONFIGURED` means the *deployment* holds no credentials for the
 *   provider -- no GitHub App id, no Slack client id -- and the start route
 *   answers 404 by design. It is not "not connected yet".
 * - `PENDING` means this workspace has claimed a GitHub installation that
 *   GitHub has not confirmed. It is not a connection, and nothing about the
 *   account or its repositories is populated while it lasts -- the server
 *   refuses to write any, because the claim is unproven. Rendering it as
 *   CONNECTED reports somebody else's organisation as this workspace's, which
 *   is the defect the status was added to stop.
 */
export type IntegrationStatus = GithubIntegration['status']

export interface UseIntegrationsResult {
  github: GithubIntegration | null
  slack: SlackIntegration | null
  teams: SettingsTeam[]
  isLoading: boolean
  errorMessage: string | null
  /** A failure of a disconnect, which belongs to neither panel's own state. */
  actionErrorMessage: string | null
  isDisconnecting: boolean
  /** True while a repository or automation write is in flight. */
  isSaving: boolean
  retry: () => void
  disconnectGithub: () => void
  disconnectSlack: () => void
  /** The WHOLE tracked set, because that is what the checkbox list means. */
  setTrackedRepositories: (repositoryIds: string[]) => void
  setAutomation: (input: {
    teamId: string
    enabled: boolean
    startedStateId?: string | null
    completedStateId?: string | null
  }) => void
}

/**
 * Both integrations, and the two ways to end one.
 *
 * ## Connecting is not here, and cannot be
 *
 * There is no `githubConnect`. Starting an OAuth flow means minting a
 * single-use CSRF `state`, putting it in a cookie and redirecting to the
 * provider, and only the server that will later check that state can do it.
 * So the screen links to the backend's own `/github/install` and
 * `/slack/oauth/start` and the browser leaves the application; no provider
 * URL, client id or scope list is assembled on this side. See
 * ./SettingsPage.tsx.
 *
 * ## Why disconnecting refetches
 *
 * Both mutations return the integration's new state, and neither
 * `GithubIntegration` nor `SlackIntegration` carries an id -- so Apollo
 * cannot normalise the result and it does not write itself into the query
 * field the screen is reading. Refetching is one line against a query that
 * costs two root fields; a hand-written `updateQuery` for each of two
 * differently-shaped payloads is four lines that can go stale.
 */
export function useIntegrations(): UseIntegrationsResult {
  const workspaceSlug = useWorkspaceSlug()
  const [actionErrorMessage, setActionErrorMessage] = useState<string | null>(null)

  const { data, error, loading, refetch } = useQuery(WorkspaceIntegrationsDocument, {
    variables: { workspaceSlug },
  })

  const integrations = { query: WorkspaceIntegrationsDocument, variables: { workspaceSlug } }

  const [github, githubState] = useMutation(WorkspaceGithubDisconnectDocument, {
    refetchQueries: [integrations],
  })
  const [slack, slackState] = useMutation(WorkspaceSlackDisconnectDocument, {
    refetchQueries: [integrations],
  })

  // Neither of these refetches. Both return `GithubIntegrationFields` -- the
  // same selection the query reads -- so Apollo would still not normalise it
  // (there is no id on `GithubIntegration`), but unlike the disconnects these
  // two carry an `errors` list that has to be read from the RESULT rather than
  // from a rejected promise. The screen renders from the result and the next
  // read comes from the cache write below.
  const [repositories, repositoriesState] = useMutation(
    WorkspaceGithubRepositoriesSetDocument,
    { refetchQueries: [integrations] },
  )
  const [automation, automationState] = useMutation(
    WorkspaceGithubAutomationSetDocument,
    { refetchQueries: [integrations] },
  )

  const retry = useCallback(() => {
    // Swallowed on purpose: `refetch` rejects *and* sets `error` on the hook
    // result, and `error` is what the screen renders.
    void refetch().catch(() => undefined)
  }, [refetch])

  const disconnectGithub = useCallback(() => {
    setActionErrorMessage(null)

    // Neither disconnect payload has an `errors` list, so a refusal -- an
    // ordinary member trying it, say -- arrives only as a rejected promise.
    void github({ variables: { input: { workspaceSlug } } }).catch((reason: unknown) => {
      setActionErrorMessage(describeError(reason))
    })
  }, [github, workspaceSlug])

  const disconnectSlack = useCallback(() => {
    setActionErrorMessage(null)

    void slack({ variables: { input: { workspaceSlug } } }).catch((reason: unknown) => {
      setActionErrorMessage(describeError(reason))
    })
  }, [slack, workspaceSlug])

  const setTrackedRepositories = useCallback(
    (repositoryIds: string[]) => {
      setActionErrorMessage(null)

      void repositories({ variables: { input: { workspaceSlug, repositoryIds } } })
        .then((result) => {
          // The refusal that matters: untracking a repository a release still
          // names. It arrives as a field error rather than a rejection, so a
          // handler that only had `.catch` would silently show the checkbox
          // ticking back on with no explanation.
          setActionErrorMessage(
            firstError(result.data?.githubRepositoriesSet.errors),
          )
        })
        .catch((reason: unknown) => {
          setActionErrorMessage(describeError(reason))
        })
    },
    [repositories, workspaceSlug],
  )

  const setAutomation = useCallback<UseIntegrationsResult['setAutomation']>(
    (input) => {
      setActionErrorMessage(null)

      void automation({ variables: { input: { workspaceSlug, ...input } } })
        .then((result) => {
          setActionErrorMessage(firstError(result.data?.githubAutomationSet.errors))
        })
        .catch((reason: unknown) => {
          setActionErrorMessage(describeError(reason))
        })
    },
    [automation, workspaceSlug],
  )

  return {
    github: data?.githubIntegration ?? null,
    slack: data?.slackIntegration ?? null,
    teams: data?.teams ?? [],
    isLoading: loading,
    errorMessage: error === undefined ? null : describeError(error),
    actionErrorMessage,
    isDisconnecting: githubState.loading || slackState.loading,
    isSaving: repositoriesState.loading || automationState.loading,
    retry,
    disconnectGithub,
    disconnectSlack,
    setTrackedRepositories,
    setAutomation,
  }
}

/**
 * The first field error a payload carried, or null for a clean write.
 *
 * Only the first: both of these mutations validate one thing at a time in
 * practice, and a screen that stacked messages would be pushing the panel
 * around for a case that does not arise. `?? null` rather than an index
 * assertion, because `noUncheckedIndexedAccess` is right that a length check
 * and an index are two statements the compiler cannot tie together.
 */
function firstError(errors: { message: string }[] | undefined): string | null {
  return errors?.[0]?.message ?? null
}
