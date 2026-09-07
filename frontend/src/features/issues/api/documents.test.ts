import { Kind } from 'graphql'
import type {
  DocumentNode,
  FieldNode,
  FragmentDefinitionNode,
  OperationDefinitionNode,
  SelectionSetNode,
} from 'graphql'
import { describe, expect, it } from 'vitest'

import { DEFAULT_PAGE_SIZE } from '../../../lib/graphql'
import { IssueCreateDocument, IssueDetailDocument, IssueListDocument } from './index'

/**
 * The shape of the documents themselves.
 *
 * Every other test in this suite drives a *controlled* link, so a document
 * that changed shape would still be answered and every screen would still
 * render. That is the right trade for behaviour tests -- but it leaves the
 * two properties below unguarded, and both fail in ways that are invisible
 * from the client.
 *
 *   - `first` must stay a literal. As a variable it has no value at
 *     validation time, so `app/graphql/limits.py` charges it at
 *     `ASSUMED_PAGE_SIZE = 100` and prices this one selection at 800 of a
 *     1000 budget. The document is then refused *during validation*, before
 *     a resolver runs -- so it presents as the whole query failing, not as a
 *     short page.
 *   - the create mutation must keep selecting a superset of the list's
 *     fields. `prependCreatedIssue` writes the mutation's issue into the
 *     cached list; if the mutation stopped selecting something the list
 *     selects, the prepended entity would be incomplete and the next read of
 *     the list would return null -- the list would go blank, silently,
 *     nowhere near the edit that caused it.
 *
 * Neither can be caught by mocking a response, because in both cases the
 * client's own code is unchanged and only the server's answer differs.
 */

function operationOf(document: DocumentNode): OperationDefinitionNode {
  const operation = document.definitions.find(
    (definition): definition is OperationDefinitionNode =>
      definition.kind === Kind.OPERATION_DEFINITION,
  )

  if (operation === undefined) {
    throw new Error('Document has no operation definition')
  }

  return operation
}

function fragmentsOf(document: DocumentNode): Map<string, FragmentDefinitionNode> {
  const fragments = new Map<string, FragmentDefinitionNode>()

  for (const definition of document.definitions) {
    if (definition.kind === Kind.FRAGMENT_DEFINITION) {
      fragments.set(definition.name.value, definition)
    }
  }

  return fragments
}

/**
 * Every field selected directly under a selection set, following fragment
 * spreads. `__typename` is excluded: `is_introspection_key` in the backend's
 * limits rule skips any field whose name starts with `__`, so the typenames
 * Apollo adds cost nothing.
 */
function fieldsOf(
  selectionSet: SelectionSetNode,
  fragments: Map<string, FragmentDefinitionNode>,
): Map<string, FieldNode> {
  const fields = new Map<string, FieldNode>()

  for (const selection of selectionSet.selections) {
    if (selection.kind === Kind.FIELD) {
      if (!selection.name.value.startsWith('__')) {
        fields.set(selection.name.value, selection)
      }
      continue
    }

    if (selection.kind === Kind.FRAGMENT_SPREAD) {
      const fragment = fragments.get(selection.name.value)

      if (fragment === undefined) {
        throw new Error(`Document spreads an undefined fragment: ${selection.name.value}`)
      }

      for (const [name, field] of fieldsOf(fragment.selectionSet, fragments)) {
        fields.set(name, field)
      }
      continue
    }

    if (selection.kind === Kind.INLINE_FRAGMENT) {
      for (const [name, field] of fieldsOf(selection.selectionSet, fragments)) {
        fields.set(name, field)
      }
    }
  }

  return fields
}

function requireField(fields: Map<string, FieldNode>, name: string): FieldNode {
  const field = fields.get(name)

  if (field === undefined) {
    throw new Error(`Expected a "${name}" field, found: ${[...fields.keys()].join(', ')}`)
  }

  return field
}

function requireSelectionSet(field: FieldNode): SelectionSetNode {
  if (field.selectionSet === undefined) {
    throw new Error(`Field "${field.name.value}" has no selection set`)
  }

  return field.selectionSet
}

/** The fields selected under one named child of `fields`. */
function childFields(
  fields: Map<string, FieldNode>,
  name: string,
  fragments: Map<string, FragmentDefinitionNode>,
): Map<string, FieldNode> {
  return fieldsOf(requireSelectionSet(requireField(fields, name)), fragments)
}

/** The same, as sorted names, which is what most assertions here compare. */
function childFieldNames(
  fields: Map<string, FieldNode>,
  name: string,
  fragments: Map<string, FragmentDefinitionNode>,
): string[] {
  return [...childFields(fields, name, fragments).keys()].toSorted()
}

/**
 * What one selection set costs the backend, by its own rules.
 *
 * `app/graphql/limits.py`: a scalar is 1, a field with a selection set is
 * `page size x (cost of that selection set)`, and the page size comes from
 * the value written in the document, then the argument's declared default,
 * then `ASSUMED_PAGE_SIZE = 100`. Fields whose name starts with `__` are
 * skipped, which is why the typenames Apollo adds are free.
 *
 * Only the first two page-size sources are implemented here, because a
 * document in this codebase that relied on the third would be the bug this
 * whole file exists to catch.
 */
function complexityOf(
  selectionSet: SelectionSetNode,
  fragments: Map<string, FragmentDefinitionNode>,
): number {
  let total = 0

  for (const [name, field] of fieldsOf(selectionSet, fragments)) {
    if (name.startsWith('__')) {
      continue
    }

    if (field.selectionSet === undefined) {
      total += 1
      continue
    }

    const first = field.arguments?.find(
      (argument) => argument.name.value === 'first',
    )
    const pageSize =
      first?.value.kind === Kind.INT ? Number(first.value.value) : 1

    total += pageSize * complexityOf(field.selectionSet, fragments)
  }

  return total
}

describe('IssueList document', () => {
  const operation = operationOf(IssueListDocument)
  const fragments = fragmentsOf(IssueListDocument)
  const issues = requireField(fieldsOf(operation.selectionSet, fragments), 'issues')

  it('writes the page size as a literal', () => {
    const first = issues.arguments?.find((argument) => argument.name.value === 'first')

    expect(first).toBeDefined()
    // An `IntValue`, not a `Variable`. This is the assertion the long note in
    // ./documents.ts exists to justify.
    expect(first?.value.kind).toBe(Kind.INT)
    expect(first?.value.kind === Kind.INT ? first.value.value : null).toBe(
      String(DEFAULT_PAGE_SIZE),
    )
  })

  it('declares the workspace, the filter and the cursor, and no page size', () => {
    const variables = (operation.variableDefinitions ?? []).map(
      (definition) => definition.variable.name.value,
    )

    // `$workspaceSlug` because the URL decides it and only a variable can
    // carry that; `$filter` because My Issues narrows the same list to one
    // assignee. No `$first`, because a variable page size is charged at
    // ASSUMED_PAGE_SIZE = 100 during validation -- see ./operations.graphql.
    // And nothing that could be an offset.
    expect(variables).toEqual(['workspaceSlug', 'filter', 'after'])
  })

  it('passes the cursor through the `after` argument', () => {
    const after = issues.arguments?.find((argument) => argument.name.value === 'after')

    expect(after?.value.kind).toBe(Kind.VARIABLE)
    expect(after?.value.kind === Kind.VARIABLE ? after.value.name.value : null).toBe(
      'after',
    )

    // The connection is `{ nodes, pageInfo }` with no per-node cursor, and
    // there is no `offset`, `skip` or `page` argument to reach for.
    const argumentNames = (issues.arguments ?? []).map(
      (argument) => argument.name.value,
    )
    expect(argumentNames.toSorted()).toEqual([
      'after',
      'filter',
      'first',
      'workspaceSlug',
    ])
  })

  it('stays inside the backend complexity budget', () => {
    /*
      The backend's own formula (`app/graphql/limits.py`, mirrored in
      `complexityOf` above because a client cannot observe it): a scalar
      costs 1, a field with a selection set costs its page size times what is
      below it, and the document is refused when the total is strictly
      greater than 1000.

      Asserting the computed cost rather than the field count is the point --
      it fails when someone adds a field, which is the change that would
      otherwise turn a client-side edit into a server-side rejection. The
      exact number is asserted as well as the ceiling, so that a change which
      halves the headroom is noticed rather than merely tolerated.
    */
    const complexity = complexityOf(operation.selectionSet, fragments)

    expect(complexity).toBeLessThanOrEqual(1000)
    expect(complexity).toBe(575)
  })

  it('selects the fields a dense row shows, and no body', () => {
    const connection = fieldsOf(requireSelectionSet(issues), fragments)

    // `description` is deliberately not something a 25-row list pays for;
    // the detail document adds it. Everything else here is drawn.
    expect(childFieldNames(connection, 'nodes', fragments)).toEqual([
      'assigneeId',
      'completedAt',
      'createdAt',
      'cycle',
      'dueDate',
      'estimate',
      'id',
      'identifier',
      'labels',
      'priority',
      'project',
      'teamId',
      'title',
      'updatedAt',
      'workflowStateId',
    ])

    // No per-node cursor exists on this connection to page by.
    expect(childFieldNames(connection, 'pageInfo', fragments)).toEqual([
      'endCursor',
      'hasNextPage',
    ])
  })
})

describe('IssueCreate document', () => {
  const operation = operationOf(IssueCreateDocument)
  const fragments = fragmentsOf(IssueCreateDocument)
  const payload = childFields(
    fieldsOf(operation.selectionSet, fragments),
    'issueCreate',
    fragments,
  )

  it('selects both failure channels', () => {
    /*
      `errors` inside the payload is expected user input being rejected, over
      an HTTP 200 with no GraphQL `errors` array. A top-level GraphQL error is
      everything else. A client that selected only one either silently
      discards validation feedback or reports an outage as a bad title.
    */
    expect([...payload.keys()].toSorted()).toEqual(['errors', 'issue'])

    expect(childFieldNames(payload, 'errors', fragments)).toEqual([
      'code',
      'field',
      'message',
    ])
  })

  it('selects a superset of what the list selects', () => {
    const listFragments = fragmentsOf(IssueListDocument)
    const listIssues = requireField(
      fieldsOf(operationOf(IssueListDocument).selectionSet, listFragments),
      'issues',
    )
    const listNodeFields = childFields(
      fieldsOf(requireSelectionSet(listIssues), listFragments),
      'nodes',
      listFragments,
    )

    const createdFields = childFields(payload, 'issue', fragments)

    for (const name of listNodeFields.keys()) {
      expect(
        createdFields.has(name),
        `The create mutation does not select "${name}", which the list selects. ` +
          'Prepending its result into the cached list would leave an incomplete ' +
          'entity and the list would read back as null.',
      ).toBe(true)
    }
  })
})

describe('IssueDetail document', () => {
  it('asks for one issue by id and selects every field', () => {
    const operation = operationOf(IssueDetailDocument)
    const fragments = fragmentsOf(IssueDetailDocument)
    const root = fieldsOf(operation.selectionSet, fragments)

    expect(
      (operation.variableDefinitions ?? []).map(
        (definition) => definition.variable.name.value,
      ),
    ).toEqual(['workspaceSlug', 'id'])

    // A strict superset of the row selection, which is what lets a mutation
    // result normalise over an entity the cached list points at.
    expect(childFieldNames(root, 'issue', fragments)).toEqual([
      'assigneeId',
      'completedAt',
      'createdAt',
      'creatorId',
      'cycle',
      'description',
      'dueDate',
      'estimate',
      'id',
      'identifier',
      'labels',
      'priority',
      'project',
      'teamId',
      'title',
      'updatedAt',
      'workflowStateId',
    ])
  })
})
