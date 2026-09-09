import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { DocumentForm } from './documents/components/DocumentForm'
import { EnvironmentForm } from './environments/components/EnvironmentForm'
import { InitiativeForm } from './initiatives/components/InitiativeForm'
import { LabelGroupForm } from './labelGroups/components/LabelGroupForm'
import { ReleaseForm } from './releases/components/ReleaseForm'
import { SavedViewForm } from './savedViews/components/SavedViewForm'
import { TemplateForm } from './templates/components/TemplateForm'

/**
 * No form may drop a refusal on the floor.
 *
 * ## The bug this file exists for
 *
 * Every one of these forms sorted `payload.errors` into a `Map` keyed by
 * field, then read out the two or three keys it draws an input for. An error
 * naming anything else -- and the server names plenty of things no form has a
 * control for -- was built into the map and never read.
 *
 * The result was the worst shape a failure can take. The request went out, the
 * server refused it, the dialog stayed open with the typed values still in it,
 * the submit button went back to enabled, and *nothing appeared anywhere on
 * the screen*. There was no message to read, no field marked invalid, and no
 * console output. The only observable difference between that and a save that
 * worked was that nothing had been saved.
 *
 * It is not a hypothetical field either. `app/services/saved_views.py` maps
 * `saved_views_creator_fk` to `{field: "workspaceSlug", code: "NOT_MEMBER"}`
 * -- what `savedViewCreate` answers when the caller was removed from the
 * workspace mid-session -- and no form anywhere renders a `workspaceSlug`
 * control. Same for `teamId` ("Team not found") on the same mutation, and for
 * every field a template or a release sends without an error slot beside it.
 *
 * ## Why the form components and not the screens
 *
 * The defect lives in the seven components, one copy each, and the fix is one
 * shared `partitionFieldErrors` they all call. Driving each screen through its
 * dialog to reach the same component would test the router seven more times
 * and the thing that broke once. `savedViews.test.tsx` carries the end-to-end
 * pass that proves the wiring reaches a real screen.
 *
 * The assertion is deliberately weak about *where* the message lands: an
 * unattached error has, by definition, no control to sit beside, and pinning
 * it to a particular element would make a later layout change fail this for
 * the wrong reason. What must hold is that it is on the screen, and that it is
 * announced -- these arrive after a submit the user is waiting on, which is
 * exactly what `role="alert"` is for.
 */

/** A refusal naming a field no form on this list draws a control for. */
const NOT_MEMBER = {
  __typename: 'ValidationErrorType' as const,
  field: 'workspaceSlug',
  code: 'NOT_MEMBER',
  message: 'You are no longer a member of this workspace',
}

const noop = () => undefined

/**
 * Each form, mounted with the least it needs to render its fields.
 *
 * `errors` and `errorMessage` are supplied per case, so the props here are
 * everything else. `ReleaseForm` gets one environment and one repository
 * because it renders a "nothing to deploy to" panel instead of a form
 * otherwise -- and that panel has no error slot at all, which is correct: it
 * has no submit button either.
 */
const FORMS: readonly {
  name: string
  render: (errors: readonly (typeof NOT_MEMBER)[], errorMessage: string | null) => void
}[] = [
  {
    name: 'DocumentForm',
    render: (errors, errorMessage) => {
      render(
        <DocumentForm
          errorMessage={errorMessage}
          errors={errors}
          initiatives={[]}
          isSaving={false}
          onCancel={noop}
          onSubmit={noop}
          projects={[]}
        />,
      )
    },
  },
  {
    name: 'EnvironmentForm',
    render: (errors, errorMessage) => {
      render(
        <EnvironmentForm
          errorMessage={errorMessage}
          errors={errors}
          isSaving={false}
          onCancel={noop}
          onSubmit={noop}
        />,
      )
    },
  },
  {
    name: 'InitiativeForm',
    render: (errors, errorMessage) => {
      render(
        <InitiativeForm
          errorMessage={errorMessage}
          errors={errors}
          isSaving={false}
          members={[]}
          onCancel={noop}
          onSubmit={noop}
          submitLabel="Create initiative"
        />,
      )
    },
  },
  {
    name: 'LabelGroupForm',
    render: (errors, errorMessage) => {
      render(
        <LabelGroupForm
          errorMessage={errorMessage}
          errors={errors}
          isSaving={false}
          onCancel={noop}
          onSubmit={noop}
          submitLabel="Create group"
        />,
      )
    },
  },
  {
    name: 'ReleaseForm',
    render: (errors, errorMessage) => {
      render(
        <ReleaseForm
          environments={[
            {
              __typename: 'Environment',
              id: '00000000-0000-4000-8000-0000000ee101',
              name: 'Production',
              kind: 'PRODUCTION',
              createdAt: '2026-01-01T00:00:00.000Z',
            },
          ]}
          errorMessage={errorMessage}
          errors={errors}
          isSaving={false}
          onCancel={noop}
          onSubmit={noop}
          repositories={[
            {
              __typename: 'GithubRepository',
              repositoryId: '90210',
              fullName: 'acme/vector',
              tracked: true,
            },
          ]}
        />,
      )
    },
  },
  {
    name: 'SavedViewForm',
    render: (errors, errorMessage) => {
      render(
        <SavedViewForm
          errorMessage={errorMessage}
          errors={errors}
          isSaving={false}
          onCancel={noop}
          onSubmit={noop}
          submitLabel="Create view"
          teams={[]}
        />,
      )
    },
  },
  {
    name: 'TemplateForm',
    render: (errors, errorMessage) => {
      render(
        <TemplateForm
          errorMessage={errorMessage}
          errors={errors}
          isSaving={false}
          onCancel={noop}
          onSubmit={noop}
          submitLabel="Create template"
          teams={[]}
          template={null}
        />,
      )
    },
  },
]

describe.each(FORMS)('$name', ({ render: mount }) => {
  it('shows a refusal naming a field it draws no control for', () => {
    mount([NOT_MEMBER], null)

    // Present at all. Before the fix this was the assertion that failed: the
    // message was read into a Map and never taken out again.
    expect(screen.getByText(NOT_MEMBER.message)).toBeInTheDocument()

    // And announced. A message that appears silently after a submit is a
    // message a screen-reader user never learns about.
    expect(screen.getByRole('alert')).toHaveTextContent(NOT_MEMBER.message)
  })

  it('shows a transport failure and an unattached refusal together', () => {
    // The two arrive on different channels and neither may hide the other:
    // dropping the transport message would be the same silent failure in the
    // other direction.
    mount([NOT_MEMBER], 'Failed to fetch')

    const alert = screen.getByRole('alert')

    expect(alert).toHaveTextContent('Failed to fetch')
    expect(alert).toHaveTextContent(NOT_MEMBER.message)
  })

  it('says nothing when the server said nothing', () => {
    // The other half of the contract. An alert region that renders empty is a
    // form that reports a failure every time it opens.
    mount([], null)

    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})
