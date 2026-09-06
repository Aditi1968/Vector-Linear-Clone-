import strawberry

from app.graphql.mutations.issues import Mutation as IssueMutation
from app.graphql.mutations.projects import ProjectMutation
from app.graphql.queries.issues import Query as IssueQuery
from app.graphql.queries.projects import ProjectQuery


@strawberry.type
class Query(IssueQuery, ProjectQuery):
    """The schema's single root query, assembled from one class per feature.

    GraphQL has exactly one Query type, and a growing product has many
    features that need fields on it. The two ways to reconcile that are one
    enormous class every feature edits, or one class per feature merged here.
    This is the second: `issues` and `projects` are added by people who never
    touch the same file, and the list of features exposed to clients is this
    class's bases, which is a thing a reader can see at a glance.

    Field order in the exported SDL follows the MRO, so it is the order of the
    bases above. That matters only because frontend/schema.graphql is a
    committed artefact compared byte for byte -- reordering these bases is a
    diff in that file and nothing more.
    """


@strawberry.type
class Mutation(IssueMutation, ProjectMutation):
    """The schema's single root mutation, assembled the same way.

    `issueSetProject` arrives through ProjectMutation rather than
    IssueMutation. It writes `issues`, and its service method lives on
    IssueService for that reason -- but it exists because projects do, it is
    meaningless without them, and a client looking for "how do I put an issue
    in a project" looks at the projects API.
    """
