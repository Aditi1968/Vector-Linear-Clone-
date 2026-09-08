/**
 * GENERATED FILE -- DO NOT EDIT.
 *
 * Written by `npm run graphql:codegen` from:
 *   - frontend/schema.graphql                                (the backend's schema)
 *   - frontend/src/features/issues/api/operations.graphql    (the operations)
 *
 * Edit those, then regenerate. `npm run graphql:check` fails when this file
 * does not match them, so an edit made here does not survive review.
 */

export type Maybe<T> = T | null;
export type InputMaybe<T> = Maybe<T>;
/** All built-in and custom scalars, mapped to their actual values */
export type Scalars = {
  ID: { input: string; output: string; }
  String: { input: string; output: string; }
  Boolean: { input: boolean; output: boolean; }
  Int: { input: number; output: number; }
  Float: { input: number; output: number; }
  /** Date (isoformat) */
  Date: { input: string; output: string; }
  /** Date with time (isoformat) */
  DateTime: { input: string; output: string; }
  /** The `JSON` scalar type represents JSON values as specified by [ECMA-404](https://ecma-international.org/wp-content/uploads/ECMA-404_2nd_edition_december_2017.pdf). */
  JSON: { input: unknown; output: unknown; }
  UUID: { input: string; output: string; }
};

/** One person's share of the unfinished work. A null `assigneeId` is the unassigned pile, which is a real bucket and usually the largest. */
export type AssigneeWorkload = {
  __typename?: 'AssigneeWorkload';
  assigneeId?: Maybe<Scalars['UUID']['output']>;
  /** The assignee's display name, falling back to their email address when they have not set one. Null with a null `assigneeId`. */
  name?: Maybe<Scalars['String']['output']>;
  /** Live issues in a BACKLOG, UNSTARTED or STARTED state. Finished work is excluded: this is what somebody is carrying, not what they have ever shipped. */
  openIssues: Scalars['Int']['output'];
};

export type Comment = {
  __typename?: 'Comment';
  authorId: Scalars['UUID']['output'];
  body: Scalars['String']['output'];
  createdAt: Scalars['DateTime']['output'];
  editedAt?: Maybe<Scalars['DateTime']['output']>;
  id: Scalars['UUID']['output'];
  issueId: Scalars['UUID']['output'];
  updatedAt: Scalars['DateTime']['output'];
};

export type CommentConnection = {
  __typename?: 'CommentConnection';
  nodes: Array<Comment>;
  pageInfo: PageInfo;
};

export type CommentCreateInput = {
  body: Scalars['String']['input'];
  issueId: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type CommentCreatePayload = {
  __typename?: 'CommentCreatePayload';
  comment?: Maybe<Comment>;
  errors: Array<ValidationErrorType>;
};

export type CommentDeleteInput = {
  id: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type CommentDeletePayload = {
  __typename?: 'CommentDeletePayload';
  deletedCommentId?: Maybe<Scalars['UUID']['output']>;
  errors: Array<ValidationErrorType>;
};

export type Cycle = {
  __typename?: 'Cycle';
  createdAt: Scalars['DateTime']['output'];
  endsAt: Scalars['DateTime']['output'];
  id: Scalars['UUID']['output'];
  name?: Maybe<Scalars['String']['output']>;
  number: Scalars['Int']['output'];
  startsAt: Scalars['DateTime']['output'];
  /** The team this cycle belongs to. Fixed for the cycle's life -- a cycle cannot be moved between teams -- so a route or a cache may be keyed on it. */
  teamId: Scalars['UUID']['output'];
  updatedAt: Scalars['DateTime']['output'];
};

export type CycleCreateInput = {
  endsAt: Scalars['DateTime']['input'];
  name?: InputMaybe<Scalars['String']['input']>;
  number: Scalars['Int']['input'];
  startsAt: Scalars['DateTime']['input'];
  teamId: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type CycleDeletePayload = {
  __typename?: 'CycleDeletePayload';
  deletedCycleId?: Maybe<Scalars['UUID']['output']>;
  errors: Array<ValidationErrorType>;
};

export type CyclePayload = {
  __typename?: 'CyclePayload';
  cycle?: Maybe<Cycle>;
  errors: Array<ValidationErrorType>;
};

/** One cycle's scope and delivery, in its own team's estimate unit. Counts are over the cycle's WHOLE scope, not the part of it inside the requested window -- a sprint's progress is the sprint's. Cycles are listed when their span overlaps the window. */
export type CycleProgress = {
  __typename?: 'CycleProgress';
  completed: Scalars['Int']['output'];
  /** The sum of the completed issues' estimates, in this team's unit. Null for a t-shirt team -- the stored integer is a rung on a ladder, so two of them do not add to a size -- and null when nothing was estimated. Read `estimated` to tell the two apart. Never add this across cycles: two cycles may be on two scales. */
  completedEstimate?: Maybe<Scalars['Int']['output']>;
  cycleId: Scalars['UUID']['output'];
  endsAt: Scalars['DateTime']['output'];
  estimateScale: EstimateScale;
  /** How many of those completions carried an estimate at all. */
  estimated: Scalars['Int']['output'];
  /** Live issues currently in this cycle. */
  issues: Scalars['Int']['output'];
  name?: Maybe<Scalars['String']['output']>;
  number: Scalars['Int']['output'];
  startsAt: Scalars['DateTime']['output'];
  /** The owning team's key. A cycle belongs to exactly one team, which is why a single estimate scale applies to the whole row. */
  teamKey: Scalars['String']['output'];
};

/** How long delivered work spent BEING WORKED ON -- first transition into a started state, to completion -- together with how much of the work that covers. This schema has no `started_at` column, so the start instant is read from the activity history, which begins at one migration and records nothing for an issue dragged straight from backlog to done. Compare `measured` against `completedTotal` before quoting the median, and read `leadTime` where the coverage is thin. */
export type CycleTimeSummary = {
  __typename?: 'CycleTimeSummary';
  /** Completed issues in this window, measurable or not. The denominator of the coverage, and the same number as `leadTime.count`. */
  completedTotal: Scalars['Int']['output'];
  /** Completed issues that had a recorded start, and so are in the percentiles. Zero is a real answer: it means no delivered issue in this window has a usable start, and the two hour figures below are then meaningless and must not be shown. */
  measured: Scalars['Int']['output'];
  medianHours: Scalars['Float']['output'];
  /** The 90th percentile, interpolated between samples. */
  p90Hours: Scalars['Float']['output'];
};

export type CycleUpdateInput = {
  endsAt: Scalars['DateTime']['input'];
  id: Scalars['UUID']['input'];
  name?: InputMaybe<Scalars['String']['input']>;
  number: Scalars['Int']['input'];
  startsAt: Scalars['DateTime']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type Document = {
  __typename?: 'Document';
  comments: DocumentCommentConnection;
  content?: Maybe<Scalars['JSON']['output']>;
  createdAt: Scalars['DateTime']['output'];
  creatorId: Scalars['UUID']['output'];
  id: Scalars['UUID']['output'];
  initiativeId?: Maybe<Scalars['UUID']['output']>;
  lastEditedBy: Scalars['UUID']['output'];
  projectId?: Maybe<Scalars['UUID']['output']>;
  revisions: DocumentRevisionConnection;
  title: Scalars['String']['output'];
  updatedAt: Scalars['DateTime']['output'];
};


export type DocumentCommentsArgs = {
  after?: InputMaybe<Scalars['String']['input']>;
  first?: Scalars['Int']['input'];
};


export type DocumentRevisionsArgs = {
  after?: InputMaybe<Scalars['String']['input']>;
  first?: Scalars['Int']['input'];
};

export type DocumentComment = {
  __typename?: 'DocumentComment';
  authorId: Scalars['UUID']['output'];
  body: Scalars['String']['output'];
  createdAt: Scalars['DateTime']['output'];
  documentId: Scalars['UUID']['output'];
  editedAt?: Maybe<Scalars['DateTime']['output']>;
  id: Scalars['UUID']['output'];
  updatedAt: Scalars['DateTime']['output'];
};

export type DocumentCommentConnection = {
  __typename?: 'DocumentCommentConnection';
  nodes: Array<DocumentComment>;
  pageInfo: PageInfo;
};

export type DocumentCommentCreateInput = {
  body: Scalars['String']['input'];
  documentId: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type DocumentCommentDeleteInput = {
  id: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type DocumentCommentDeletePayload = {
  __typename?: 'DocumentCommentDeletePayload';
  deletedCommentId?: Maybe<Scalars['UUID']['output']>;
  errors: Array<ValidationErrorType>;
};

export type DocumentCommentPayload = {
  __typename?: 'DocumentCommentPayload';
  comment?: Maybe<DocumentComment>;
  errors: Array<ValidationErrorType>;
};

export type DocumentConnection = {
  __typename?: 'DocumentConnection';
  nodes: Array<Document>;
  pageInfo: PageInfo;
};

export type DocumentCreateInput = {
  content?: InputMaybe<Scalars['JSON']['input']>;
  initiativeId?: InputMaybe<Scalars['UUID']['input']>;
  projectId?: InputMaybe<Scalars['UUID']['input']>;
  title: Scalars['String']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type DocumentDeleteInput = {
  id: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type DocumentDeletePayload = {
  __typename?: 'DocumentDeletePayload';
  deletedDocumentId?: Maybe<Scalars['UUID']['output']>;
  errors: Array<ValidationErrorType>;
};

export type DocumentEditInput = {
  content?: InputMaybe<Scalars['JSON']['input']>;
  id: Scalars['UUID']['input'];
  snapshot?: Scalars['Boolean']['input'];
  title?: InputMaybe<Scalars['String']['input']>;
  workspaceSlug: Scalars['String']['input'];
};

export type DocumentPayload = {
  __typename?: 'DocumentPayload';
  document?: Maybe<Document>;
  errors: Array<ValidationErrorType>;
};

export type DocumentRestoreInput = {
  documentId: Scalars['UUID']['input'];
  revisionId: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type DocumentRevision = {
  __typename?: 'DocumentRevision';
  authorId: Scalars['UUID']['output'];
  content?: Maybe<Scalars['JSON']['output']>;
  createdAt: Scalars['DateTime']['output'];
  documentId: Scalars['UUID']['output'];
  id: Scalars['UUID']['output'];
  title: Scalars['String']['output'];
};

export type DocumentRevisionConnection = {
  __typename?: 'DocumentRevisionConnection';
  nodes: Array<DocumentRevision>;
  pageInfo: PageInfo;
};

/** A slice of the calendar, resolved by the SERVER against its own today. OVERDUE is strictly before today and never includes the undated -- an issue with no due date has missed nothing. TODAY is that day. THIS_WEEK is today and the six days after it, a rolling seven rather than a Monday-to-Sunday week, which would be nearly empty by Friday afternoon. NO_DUE_DATE is the issues that have committed to no day, which is most of them and is an ordinary state rather than missing data. Today is UTC and is the same day for everyone in the workspace: a due date is a calendar day with no timezone, so resolving it against each viewer's local today would make one issue overdue for one colleague and not another. */
export type DueWindow =
  | 'NO_DUE_DATE'
  | 'OVERDUE'
  | 'THIS_WEEK'
  | 'TODAY';

export type DuplicateSuggestion = {
  __typename?: 'DuplicateSuggestion';
  issue: Issue;
  similarity: Scalars['Float']['output'];
};

/** A distribution of durations in hours: median and 90th percentile, with the sample size they are over. Percentiles rather than a mean, because one two-year-old issue moves a mean by more than a week's real work. */
export type DurationSummary = {
  __typename?: 'DurationSummary';
  /** How many issues the two percentiles are over. A median over three issues is a number that will move next week; this is what lets a reader see that. */
  count: Scalars['Int']['output'];
  medianHours: Scalars['Float']['output'];
  /** The 90th percentile, interpolated between samples. */
  p90Hours: Scalars['Float']['output'];
};

export type EmbeddingIndexingState = {
  __typename?: 'EmbeddingIndexingState';
  enabled: Scalars['Boolean']['output'];
  failed: Scalars['Int']['output'];
  indexed: Scalars['Int']['output'];
  pending: Scalars['Int']['output'];
};

export type Environment = {
  __typename?: 'Environment';
  createdAt: Scalars['DateTime']['output'];
  id: Scalars['UUID']['output'];
  kind: EnvironmentKind;
  name: Scalars['String']['output'];
};

export type EnvironmentCreateInput = {
  kind: EnvironmentKind;
  name: Scalars['String']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type EnvironmentKind =
  | 'CUSTOM'
  | 'DEVELOPMENT'
  | 'PRODUCTION'
  | 'STAGING';

export type EnvironmentPayload = {
  __typename?: 'EnvironmentPayload';
  environment?: Maybe<Environment>;
  errors: Array<ValidationErrorType>;
};

/** What a team's estimates count. NONE is a whole number with no unit named, which is what every estimate written before this setting existed means. POINTS and HOURS are units and put no ceiling on the value. TSHIRT is a LADDER rather than a quantity: the stored integer is a position, 1 through 5, rendered XS, S, M, L, XL -- so a team on that scale can only write those five numbers. */
export type EstimateScale =
  | 'HOURS'
  | 'NONE'
  | 'POINTS'
  | 'TSHIRT';

/** One shortcut in one person's sidebar. Exactly one of `teamId`, `projectId` and `savedViewId` is set. */
export type Favorite = {
  __typename?: 'Favorite';
  createdAt: Scalars['DateTime']['output'];
  id: Scalars['UUID']['output'];
  /** Where this sits in the person's own list. Neither unique nor contiguous: ties are broken by id, so two favorites sharing a number are ordered stably rather than ambiguously. */
  position: Scalars['Int']['output'];
  projectId?: Maybe<Scalars['UUID']['output']>;
  savedViewId?: Maybe<Scalars['UUID']['output']>;
  teamId?: Maybe<Scalars['UUID']['output']>;
};

/** Exactly one of `teamId`, `projectId` and `savedViewId` must be supplied. Sending none or more than one is a field error rather than a guess at which was meant. */
export type FavoriteAddInput = {
  projectId?: InputMaybe<Scalars['UUID']['input']>;
  savedViewId?: InputMaybe<Scalars['UUID']['input']>;
  teamId?: InputMaybe<Scalars['UUID']['input']>;
  workspaceSlug: Scalars['String']['input'];
};

export type FavoriteDeletePayload = {
  __typename?: 'FavoriteDeletePayload';
  deletedFavoriteId?: Maybe<Scalars['UUID']['output']>;
  errors: Array<ValidationErrorType>;
};

export type FavoritePayload = {
  __typename?: 'FavoritePayload';
  errors: Array<ValidationErrorType>;
  favorite?: Maybe<Favorite>;
};

export type FavoriteRemoveInput = {
  id: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type FavoriteReorderInput = {
  id: Scalars['UUID']['input'];
  position: Scalars['Int']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type GithubAutomationSetInput = {
  completedStateId?: InputMaybe<Scalars['UUID']['input']>;
  enabled: Scalars['Boolean']['input'];
  startedStateId?: InputMaybe<Scalars['UUID']['input']>;
  teamId: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type GithubCommit = {
  __typename?: 'GithubCommit';
  committedAt?: Maybe<Scalars['DateTime']['output']>;
  /** The whole commit message. */
  message: Scalars['String']['output'];
  repository: Scalars['String']['output'];
  repositoryId: Scalars['ID']['output'];
  /** The full 40-character SHA, which is the commit's identity. */
  sha: Scalars['String']['output'];
  /** The first seven characters, which is what a list shows. A rendering and never an identity: the abbreviation is ambiguous by construction. */
  shortSha: Scalars['String']['output'];
  /** The message's first line, which is what a list shows. */
  summary: Scalars['String']['output'];
  url?: Maybe<Scalars['String']['output']>;
};

export type GithubDevelopment = {
  __typename?: 'GithubDevelopment';
  /** A deterministic branch name for this issue -- eng-142-fix-slack-oauth-callback. Safe to paste into `git checkout -b` for any title: unicode is folded, punctuation collapses, and a title that folds to nothing leaves the identifier alone. Creating it needs no GitHub permission and this field creates nothing; the identifier leads so that a pull request opened from the branch links back without anyone typing the identifier twice. */
  branchName: Scalars['String']['output'];
  commits: Array<GithubCommit>;
  pullRequests: Array<GithubPullRequest>;
};


export type GithubDevelopmentCommitsArgs = {
  first?: Scalars['Int']['input'];
};


export type GithubDevelopmentPullRequestsArgs = {
  first?: Scalars['Int']['input'];
};

export type GithubDisconnectInput = {
  workspaceSlug: Scalars['String']['input'];
};

export type GithubIntegration = {
  __typename?: 'GithubIntegration';
  accountLogin?: Maybe<Scalars['String']['output']>;
  automations: Array<GithubIssueAutomation>;
  connectedAt?: Maybe<Scalars['DateTime']['output']>;
  connectedById?: Maybe<Scalars['UUID']['output']>;
  repositories: Array<GithubRepository>;
  status: GithubIntegrationStatus;
};

export type GithubIntegrationPayload = {
  __typename?: 'GithubIntegrationPayload';
  errors: Array<ValidationErrorType>;
  integration?: Maybe<GithubIntegration>;
};

export type GithubIntegrationStatus =
  | 'CONNECTED'
  | 'DISCONNECTED'
  | 'PENDING'
  | 'UNCONFIGURED';

/** What a pull request does to one team's issues. Present only for teams that have turned it on; an absent team is a team with no automation, which is the default. */
export type GithubIssueAutomation = {
  __typename?: 'GithubIssueAutomation';
  /** Where an issue goes when a pull request naming it MERGES. A pull request closed without merging moves nothing: an abandoned attempt is not shipped work. */
  completedStateId?: Maybe<Scalars['UUID']['output']>;
  /** Where an issue goes when a pull request naming it opens, reopens, or is marked ready for review. A pull request opened as a DRAFT moves nothing -- a draft says the work is not ready. */
  startedStateId?: Maybe<Scalars['UUID']['output']>;
  teamId: Scalars['UUID']['output'];
};

export type GithubLinkSource =
  | 'BODY'
  | 'BRANCH'
  | 'TITLE';

export type GithubPullRequest = {
  __typename?: 'GithubPullRequest';
  /** The branch the pull request is from, or null where the payload omitted it -- a deleted or cross-fork head. */
  branch?: Maybe<Scalars['String']['output']>;
  /** Which of the pull request's title, body and branch name this issue's identifier appears in. More than one is ordinary, and each is retracted independently: editing the title removes the title's link and leaves the branch's. */
  linkedBy: Array<GithubLinkSource>;
  mergedAt?: Maybe<Scalars['DateTime']['output']>;
  number: Scalars['Int']['output'];
  /** The repository this pull request is on, as owner/name. */
  repository: Scalars['String']['output'];
  repositoryId: Scalars['ID']['output'];
  state: GithubPullRequestState;
  title: Scalars['String']['output'];
  /** GitHub's own updated_at, not Vector's. Null where the payload carried none. */
  updatedAt?: Maybe<Scalars['DateTime']['output']>;
  /** The link GitHub reported, stored rather than rebuilt from the owner, name and number: GitHub owns its URL layout, and a rebuilt link is a guess that breaks silently. */
  url?: Maybe<Scalars['String']['output']>;
};

export type GithubPullRequestState =
  | 'CLOSED'
  | 'DRAFT'
  | 'MERGED'
  | 'OPEN';

export type GithubRepositoriesSetInput = {
  repositoryIds: Array<Scalars['ID']['input']>;
  workspaceSlug: Scalars['String']['input'];
};

export type GithubRepository = {
  __typename?: 'GithubRepository';
  fullName: Scalars['String']['output'];
  repositoryId: Scalars['ID']['output'];
  /** Whether this workspace applies GitHub deliveries about this repository. False is a choice an admin made here, not something GitHub said: the installation still covers it, and Vector is declining the pull requests and pushes. Development history already collected is kept and still shown. */
  tracked: Scalars['Boolean']['output'];
};

export type Health =
  | 'AT_RISK'
  | 'OFF_TRACK'
  | 'ON_TRACK';

export type Initiative = {
  __typename?: 'Initiative';
  childInitiativeIds: Array<Scalars['UUID']['output']>;
  createdAt: Scalars['DateTime']['output'];
  description?: Maybe<Scalars['String']['output']>;
  health?: Maybe<Health>;
  id: Scalars['UUID']['output'];
  name: Scalars['String']['output'];
  ownerId?: Maybe<Scalars['UUID']['output']>;
  parentInitiativeId?: Maybe<Scalars['UUID']['output']>;
  projectIds: Array<Scalars['UUID']['output']>;
  status: InitiativeStatus;
  targetDate?: Maybe<Scalars['Date']['output']>;
  updatedAt: Scalars['DateTime']['output'];
  updates: Array<InitiativeUpdate>;
};

export type InitiativeClearParentInput = {
  initiativeId: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type InitiativeConnection = {
  __typename?: 'InitiativeConnection';
  nodes: Array<Initiative>;
  pageInfo: PageInfo;
};

export type InitiativeCreateInput = {
  description?: InputMaybe<Scalars['String']['input']>;
  name: Scalars['String']['input'];
  ownerId?: InputMaybe<Scalars['UUID']['input']>;
  status?: InitiativeStatus;
  targetDate?: InputMaybe<Scalars['Date']['input']>;
  workspaceSlug: Scalars['String']['input'];
};

export type InitiativeDeleteInput = {
  id: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type InitiativeDeletePayload = {
  __typename?: 'InitiativeDeletePayload';
  deletedInitiativeId?: Maybe<Scalars['UUID']['output']>;
  errors: Array<ValidationErrorType>;
};

export type InitiativePayload = {
  __typename?: 'InitiativePayload';
  errors: Array<ValidationErrorType>;
  initiative?: Maybe<Initiative>;
};

export type InitiativeProjectInput = {
  initiativeId: Scalars['UUID']['input'];
  projectId: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type InitiativeSetParentInput = {
  initiativeId: Scalars['UUID']['input'];
  parentInitiativeId: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type InitiativeStatus =
  | 'ACTIVE'
  | 'CANCELED'
  | 'COMPLETED'
  | 'PLANNED';

export type InitiativeUpdate = {
  __typename?: 'InitiativeUpdate';
  authorId: Scalars['UUID']['output'];
  body: Scalars['String']['output'];
  createdAt: Scalars['DateTime']['output'];
  health: Health;
  id: Scalars['UUID']['output'];
  initiativeId: Scalars['UUID']['output'];
};

export type InitiativeUpdateInput = {
  description?: InputMaybe<Scalars['String']['input']>;
  id: Scalars['UUID']['input'];
  name?: InputMaybe<Scalars['String']['input']>;
  ownerId?: InputMaybe<Scalars['UUID']['input']>;
  status?: InputMaybe<InitiativeStatus>;
  targetDate?: InputMaybe<Scalars['Date']['input']>;
  workspaceSlug: Scalars['String']['input'];
};

export type InitiativeUpdatePayload = {
  __typename?: 'InitiativeUpdatePayload';
  errors: Array<ValidationErrorType>;
  update?: Maybe<InitiativeUpdate>;
};

export type InitiativeUpdatePostInput = {
  body: Scalars['String']['input'];
  health: Health;
  initiativeId: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type InvitationAcceptInput = {
  token: Scalars['String']['input'];
};

export type InvitationAcceptPayload = {
  __typename?: 'InvitationAcceptPayload';
  errors: Array<ValidationErrorType>;
  workspace?: Maybe<Workspace>;
};

export type InvitationCreateInput = {
  email: Scalars['String']['input'];
  role: WorkspaceRole;
  workspaceSlug: Scalars['String']['input'];
};

export type InvitationCreatePayload = {
  __typename?: 'InvitationCreatePayload';
  errors: Array<ValidationErrorType>;
  invitation?: Maybe<WorkspaceInvitation>;
  token?: Maybe<Scalars['String']['output']>;
};

export type InvitationRevokeInput = {
  id: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type InvitationRevokePayload = {
  __typename?: 'InvitationRevokePayload';
  errors: Array<ValidationErrorType>;
  revokedInvitationId?: Maybe<Scalars['UUID']['output']>;
};

export type Issue = {
  __typename?: 'Issue';
  activity: IssueActivityConnection;
  /** When the issue was taken off the board. Always null here, because archived issues are absent from every query -- only the archive mutation's own result carries a value. */
  archivedAt?: Maybe<Scalars['DateTime']['output']>;
  assigneeId?: Maybe<Scalars['UUID']['output']>;
  children: IssueSummaryConnection;
  comments: CommentConnection;
  /** When the issue stopped being worked on. Derived from the workflow state's category and not settable directly: it is non-null exactly while the issue sits in a completed or canceled state. */
  completedAt?: Maybe<Scalars['DateTime']['output']>;
  createdAt: Scalars['DateTime']['output'];
  /** Who filed the issue, or null where that is not known -- an issue created before accounts existed, or one whose author's account has since been deleted. */
  creatorId?: Maybe<Scalars['UUID']['output']>;
  cycle?: Maybe<Cycle>;
  description?: Maybe<Scalars['String']['output']>;
  development: GithubDevelopment;
  /** A calendar day, not an instant: the same day for every viewer, in every timezone. */
  dueDate?: Maybe<Scalars['Date']['output']>;
  estimate?: Maybe<Scalars['Int']['output']>;
  id: Scalars['UUID']['output'];
  /** The name this issue is known by outside the product -- ENG-42. Its team's key, a hyphen, and the issue's number. */
  identifier: Scalars['String']['output'];
  labels: Array<Label>;
  milestoneId?: Maybe<Scalars['UUID']['output']>;
  /** Sequential within the team and never reused. Unique only alongside the team; two teams both have a number 42. */
  number: Scalars['Int']['output'];
  parent?: Maybe<IssueSummary>;
  priority: Scalars['Int']['output'];
  project?: Maybe<Project>;
  projectId?: Maybe<Scalars['UUID']['output']>;
  relations: IssueRelationConnection;
  teamId: Scalars['UUID']['output'];
  title: Scalars['String']['output'];
  updatedAt: Scalars['DateTime']['output'];
  workflowStateId: Scalars['UUID']['output'];
};


export type IssueActivityArgs = {
  after?: InputMaybe<Scalars['String']['input']>;
  first?: Scalars['Int']['input'];
};


export type IssueChildrenArgs = {
  after?: InputMaybe<Scalars['String']['input']>;
  first?: Scalars['Int']['input'];
};


export type IssueCommentsArgs = {
  after?: InputMaybe<Scalars['String']['input']>;
  first?: Scalars['Int']['input'];
};


export type IssueRelationsArgs = {
  after?: InputMaybe<Scalars['String']['input']>;
  first?: Scalars['Int']['input'];
};

export type IssueActivity = {
  __typename?: 'IssueActivity';
  actorId?: Maybe<Scalars['UUID']['output']>;
  /** What caused this, when `actorId` is null and it was not nobody. Null for everything a person did -- the actor is the cause. Opaque text with a documented prefix; today the only one is `github_pull_request:owner/name#84`, written when a pull request moved the issue. A client that does not recognise a prefix should render the row exactly as it renders one with no cause, which is what keeps a second cause from being a breaking change. */
  causedBy?: Maybe<Scalars['String']['output']>;
  createdAt: Scalars['DateTime']['output'];
  fromValue?: Maybe<Scalars['String']['output']>;
  id: Scalars['UUID']['output'];
  issueId: Scalars['UUID']['output'];
  kind: IssueActivityKind;
  toValue?: Maybe<Scalars['String']['output']>;
};

export type IssueActivityConnection = {
  __typename?: 'IssueActivityConnection';
  nodes: Array<IssueActivity>;
  pageInfo: PageInfo;
};

export type IssueActivityKind =
  | 'ARCHIVED'
  | 'ASSIGNEE_CHANGED'
  | 'COMMENTED'
  | 'CREATED'
  | 'CYCLE_CHANGED'
  | 'LABEL_ATTACHED'
  | 'LABEL_DETACHED'
  | 'PRIORITY_CHANGED'
  | 'PROJECT_CHANGED'
  | 'RELATION_ADDED'
  | 'STATE_CHANGED'
  | 'TITLE_CHANGED';

export type IssueArchivePayload = {
  __typename?: 'IssueArchivePayload';
  errors: Array<ValidationErrorType>;
  issue?: Maybe<Issue>;
};

export type IssueBulkArchiveInput = {
  issueIds: Array<Scalars['UUID']['input']>;
  workspaceSlug: Scalars['String']['input'];
};

export type IssueBulkPayload = {
  __typename?: 'IssueBulkPayload';
  count: Scalars['Int']['output'];
  errors: Array<ValidationErrorType>;
  issues: Array<IssueSummary>;
};

/** One change applied to many issues, or to none of them. Every field is optional and omitting one leaves that value alone on every issue. At most 100 issues may be named; a longer list is refused rather than truncated. */
export type IssueBulkUpdateInput = {
  addLabelIds?: Array<Scalars['UUID']['input']>;
  assigneeId?: InputMaybe<Scalars['UUID']['input']>;
  cycleId?: InputMaybe<Scalars['UUID']['input']>;
  dueDate?: InputMaybe<Scalars['Date']['input']>;
  estimate?: InputMaybe<Scalars['Int']['input']>;
  issueIds: Array<Scalars['UUID']['input']>;
  milestoneId?: InputMaybe<Scalars['UUID']['input']>;
  priority?: InputMaybe<Scalars['Int']['input']>;
  projectId?: InputMaybe<Scalars['UUID']['input']>;
  removeLabelIds?: Array<Scalars['UUID']['input']>;
  workflowStateId?: InputMaybe<Scalars['UUID']['input']>;
  workspaceSlug: Scalars['String']['input'];
};

export type IssueClearParentInput = {
  issueId: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type IssueConnection = {
  __typename?: 'IssueConnection';
  nodes: Array<Issue>;
  pageInfo: PageInfo;
  /** How many live issues match, ignoring paging -- the number a column header states, as opposed to how many have been loaded. */
  totalCount: Scalars['Int']['output'];
};

export type IssueCreateFromTemplateInput = {
  teamId: Scalars['UUID']['input'];
  templateId: Scalars['UUID']['input'];
  title?: InputMaybe<Scalars['String']['input']>;
  workspaceSlug: Scalars['String']['input'];
};

export type IssueCreateFromTemplatePayload = {
  __typename?: 'IssueCreateFromTemplatePayload';
  errors: Array<ValidationErrorType>;
  issue?: Maybe<Issue>;
};

export type IssueCreateInput = {
  assigneeId?: InputMaybe<Scalars['UUID']['input']>;
  description?: InputMaybe<Scalars['String']['input']>;
  dueDate?: InputMaybe<Scalars['Date']['input']>;
  estimate?: InputMaybe<Scalars['Int']['input']>;
  priority?: Scalars['Int']['input'];
  teamId: Scalars['UUID']['input'];
  title: Scalars['String']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type IssueCreatePayload = {
  __typename?: 'IssueCreatePayload';
  errors: Array<ValidationErrorType>;
  issue?: Maybe<Issue>;
};

/** What narrows an issue list. Every field is optional and every one of them narrows: nothing here can widen a list beyond the workspace the request was authorized for, so an id belonging to another workspace selects nothing. */
export type IssueFilterInput = {
  assigneeId?: InputMaybe<Scalars['UUID']['input']>;
  cycleId?: InputMaybe<Scalars['UUID']['input']>;
  due?: InputMaybe<DueWindow>;
  dueAfter?: InputMaybe<Scalars['Date']['input']>;
  dueBefore?: InputMaybe<Scalars['Date']['input']>;
  labelId?: InputMaybe<Scalars['UUID']['input']>;
  priority?: InputMaybe<Scalars['Int']['input']>;
  projectId?: InputMaybe<Scalars['UUID']['input']>;
  stateCategory?: InputMaybe<WorkflowStateCategory>;
  teamId?: InputMaybe<Scalars['UUID']['input']>;
  workflowStateId?: InputMaybe<Scalars['UUID']['input']>;
};

export type IssueLabelInput = {
  issueId: Scalars['UUID']['input'];
  labelId: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type IssueLabelPayload = {
  __typename?: 'IssueLabelPayload';
  errors: Array<ValidationErrorType>;
  issue?: Maybe<Issue>;
};

/** What an issue list is sorted by. PRIORITY is urgency and not the raw column: 0 means no priority rather than the lowest one, so ascending is Urgent, High, Medium, Low and then the untriaged. DUE_DATE ascending puts the soonest first and the undated last. */
export type IssueOrderField =
  | 'CREATED_AT'
  | 'DUE_DATE'
  | 'PRIORITY'
  | 'UPDATED_AT';

/** How an issue list is sorted. */
export type IssueOrderInput = {
  direction?: OrderDirection;
  field?: IssueOrderField;
};

export type IssueParentPayload = {
  __typename?: 'IssueParentPayload';
  errors: Array<ValidationErrorType>;
  issue?: Maybe<Issue>;
};

/** The schedule on which a template files itself. */
export type IssueRecurrence = {
  __typename?: 'IssueRecurrence';
  /** 1-31 for MONTHLY, null otherwise. The 31st is CLAMPED to the last day of a shorter month rather than skipping it -- and the clamp is applied to this number every month rather than to the previous instance, so February lands on the 28th and March returns to the 31st. */
  dayOfMonth?: Maybe<Scalars['Int']['output']>;
  /** The generated issue's due date, as days after the day it is filed. Null for a recurring issue with no due date. Counted from the filing day and not from the scheduled one, so an issue filed late by a sweep that was down is not born overdue. */
  dueInDays?: Maybe<Scalars['Int']['output']>;
  frequency: RecurrenceFrequency;
  /** The N in 'every N days / weeks / months'. At least 1. */
  intervalCount: Scalars['Int']['output'];
  /** The day the next issue will be filed. */
  nextRunOn: Scalars['Date']['output'];
  /** The day the schedule is anchored to and the first it may fire. Load-bearing above an interval of 1: 'every two weeks on Tuesday' does not say which Tuesdays without a week to count from. */
  startsOn: Scalars['Date']['output'];
  /** Which team the generated issue lands in. Required even for a workspace-wide template, because an issue belongs to a team and the sweep that files it has no caller to ask. */
  teamId: Scalars['UUID']['output'];
  /** ISO weekday numbers, 1 = Monday through 7 = Sunday. Non-empty for WEEKLY and empty for the other two. */
  weekdays: Array<Scalars['Int']['output']>;
};

export type IssueRecurrenceClearInput = {
  templateId: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type IssueRecurrenceClearPayload = {
  __typename?: 'IssueRecurrenceClearPayload';
  cleared: Scalars['Boolean']['output'];
  errors: Array<ValidationErrorType>;
};

export type IssueRecurrenceSetInput = {
  dayOfMonth?: InputMaybe<Scalars['Int']['input']>;
  dueInDays?: InputMaybe<Scalars['Int']['input']>;
  frequency: RecurrenceFrequency;
  intervalCount?: Scalars['Int']['input'];
  startsOn: Scalars['Date']['input'];
  teamId: Scalars['UUID']['input'];
  templateId: Scalars['UUID']['input'];
  weekdays?: Array<Scalars['Int']['input']>;
  workspaceSlug: Scalars['String']['input'];
};

export type IssueRecurrenceSetPayload = {
  __typename?: 'IssueRecurrenceSetPayload';
  errors: Array<ValidationErrorType>;
  recurrence?: Maybe<IssueRecurrence>;
};

export type IssueRelation = {
  __typename?: 'IssueRelation';
  createdAt: Scalars['DateTime']['output'];
  id: Scalars['UUID']['output'];
  issue: IssueSummary;
  type: IssueRelationType;
};

export type IssueRelationConnection = {
  __typename?: 'IssueRelationConnection';
  nodes: Array<IssueRelation>;
  pageInfo: PageInfo;
};

export type IssueRelationCreateInput = {
  sourceIssueId: Scalars['UUID']['input'];
  targetIssueId: Scalars['UUID']['input'];
  type: IssueRelationType;
  workspaceSlug: Scalars['String']['input'];
};

export type IssueRelationCreatePayload = {
  __typename?: 'IssueRelationCreatePayload';
  errors: Array<ValidationErrorType>;
  relation?: Maybe<IssueRelation>;
};

export type IssueRelationDeleteInput = {
  id: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type IssueRelationDeletePayload = {
  __typename?: 'IssueRelationDeletePayload';
  deletedRelationId?: Maybe<Scalars['UUID']['output']>;
  errors: Array<ValidationErrorType>;
};

export type IssueRelationType =
  | 'BLOCKED_BY'
  | 'BLOCKS'
  | 'DUPLICATE'
  | 'RELATED';

export type IssueSetCycleInput = {
  cycleId?: InputMaybe<Scalars['UUID']['input']>;
  issueId: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type IssueSetCyclePayload = {
  __typename?: 'IssueSetCyclePayload';
  errors: Array<ValidationErrorType>;
  issue?: Maybe<Issue>;
};

export type IssueSetParentInput = {
  issueId: Scalars['UUID']['input'];
  parentId: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type IssueSetProjectInput = {
  issueId: Scalars['UUID']['input'];
  milestoneId?: InputMaybe<Scalars['UUID']['input']>;
  projectId?: InputMaybe<Scalars['UUID']['input']>;
  workspaceSlug: Scalars['String']['input'];
};

export type IssueSetProjectPayload = {
  __typename?: 'IssueSetProjectPayload';
  errors: Array<ValidationErrorType>;
  issue?: Maybe<Issue>;
};

export type IssueSubscriber = {
  __typename?: 'IssueSubscriber';
  /** When this person started watching, which does not move if they are auto-subscribed again. */
  createdAt: Scalars['DateTime']['output'];
  userId: Scalars['UUID']['output'];
};

export type IssueSubscriptionInput = {
  issueId: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type IssueSubscriptionPayload = {
  __typename?: 'IssueSubscriptionPayload';
  errors: Array<ValidationErrorType>;
  subscribed: Scalars['Boolean']['output'];
};

export type IssueSummary = {
  __typename?: 'IssueSummary';
  assigneeId?: Maybe<Scalars['UUID']['output']>;
  completedAt?: Maybe<Scalars['DateTime']['output']>;
  createdAt: Scalars['DateTime']['output'];
  creatorId?: Maybe<Scalars['UUID']['output']>;
  cycleId?: Maybe<Scalars['UUID']['output']>;
  description?: Maybe<Scalars['String']['output']>;
  dueDate?: Maybe<Scalars['Date']['output']>;
  estimate?: Maybe<Scalars['Int']['output']>;
  id: Scalars['UUID']['output'];
  /** The name this issue is known by outside the product -- ENG-42. Costs nothing to select: it is the team's key and the issue's number, both already on the row. */
  identifier: Scalars['String']['output'];
  milestoneId?: Maybe<Scalars['UUID']['output']>;
  priority: Scalars['Int']['output'];
  projectId?: Maybe<Scalars['UUID']['output']>;
  teamId: Scalars['UUID']['output'];
  title: Scalars['String']['output'];
  updatedAt: Scalars['DateTime']['output'];
  workflowStateId: Scalars['UUID']['output'];
};

export type IssueSummaryConnection = {
  __typename?: 'IssueSummaryConnection';
  nodes: Array<IssueSummary>;
  pageInfo: PageInfo;
};

export type IssueTemplate = {
  __typename?: 'IssueTemplate';
  assigneeId?: Maybe<Scalars['UUID']['output']>;
  createdAt: Scalars['DateTime']['output'];
  cycleId?: Maybe<Scalars['UUID']['output']>;
  description?: Maybe<Scalars['String']['output']>;
  estimate?: Maybe<Scalars['Int']['output']>;
  id: Scalars['UUID']['output'];
  labelIds: Array<Scalars['UUID']['output']>;
  name: Scalars['String']['output'];
  priority?: Maybe<Scalars['Int']['output']>;
  projectId?: Maybe<Scalars['UUID']['output']>;
  /** The schedule this template files itself on, or null for one somebody applies by hand -- which is nearly all of them. Set and cleared by their own mutations rather than by the save above: a save REPLACES the template, so a schedule carried in that input would be cleared every time somebody fixed a typo in the title. */
  recurrence?: Maybe<IssueRecurrence>;
  teamId?: Maybe<Scalars['UUID']['output']>;
  title?: Maybe<Scalars['String']['output']>;
  updatedAt: Scalars['DateTime']['output'];
};

export type IssueTemplateCreateInput = {
  template: IssueTemplateFieldsInput;
  workspaceSlug: Scalars['String']['input'];
};

export type IssueTemplateDeleteInput = {
  id: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type IssueTemplateDeletePayload = {
  __typename?: 'IssueTemplateDeletePayload';
  errors: Array<ValidationErrorType>;
  id?: Maybe<Scalars['UUID']['output']>;
};

export type IssueTemplateFieldsInput = {
  assigneeId?: InputMaybe<Scalars['UUID']['input']>;
  cycleId?: InputMaybe<Scalars['UUID']['input']>;
  description?: InputMaybe<Scalars['String']['input']>;
  estimate?: InputMaybe<Scalars['Int']['input']>;
  labelIds?: Array<Scalars['UUID']['input']>;
  name: Scalars['String']['input'];
  priority?: InputMaybe<Scalars['Int']['input']>;
  projectId?: InputMaybe<Scalars['UUID']['input']>;
  teamId?: InputMaybe<Scalars['UUID']['input']>;
  title?: InputMaybe<Scalars['String']['input']>;
};

export type IssueTemplateSavePayload = {
  __typename?: 'IssueTemplateSavePayload';
  errors: Array<ValidationErrorType>;
  template?: Maybe<IssueTemplate>;
};

export type IssueTemplateUpdateInput = {
  id: Scalars['UUID']['input'];
  template: IssueTemplateFieldsInput;
  workspaceSlug: Scalars['String']['input'];
};

export type IssueUpdateInput = {
  assigneeId?: InputMaybe<Scalars['UUID']['input']>;
  description?: InputMaybe<Scalars['String']['input']>;
  dueDate?: InputMaybe<Scalars['Date']['input']>;
  estimate?: InputMaybe<Scalars['Int']['input']>;
  priority?: InputMaybe<Scalars['Int']['input']>;
  title?: InputMaybe<Scalars['String']['input']>;
  workflowStateId?: InputMaybe<Scalars['UUID']['input']>;
  workspaceSlug: Scalars['String']['input'];
};

export type IssueUpdatePayload = {
  __typename?: 'IssueUpdatePayload';
  errors: Array<ValidationErrorType>;
  issue?: Maybe<Issue>;
};

export type Label = {
  __typename?: 'Label';
  color: Scalars['String']['output'];
  createdAt: Scalars['DateTime']['output'];
  /** The label group this label belongs to, or null for one that belongs to none. An id rather than the group itself: a label picker renders a hundred of these and would otherwise resolve a group per row, and the groups are already on the page through `labelGroups`. */
  groupId?: Maybe<Scalars['UUID']['output']>;
  id: Scalars['UUID']['output'];
  name: Scalars['String']['output'];
  updatedAt: Scalars['DateTime']['output'];
};

export type LabelConnection = {
  __typename?: 'LabelConnection';
  nodes: Array<Label>;
  pageInfo: PageInfo;
};

export type LabelCreateInput = {
  color?: InputMaybe<Scalars['String']['input']>;
  name: Scalars['String']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type LabelDeleteInput = {
  id: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type LabelDeletePayload = {
  __typename?: 'LabelDeletePayload';
  deletedLabelId?: Maybe<Scalars['UUID']['output']>;
  errors: Array<ValidationErrorType>;
};

export type LabelGroup = {
  __typename?: 'LabelGroup';
  createdAt: Scalars['DateTime']['output'];
  /** Whether an issue may wear at most one label from this group. Enforced by the database, not by this server: attaching a second label from an exclusive group is refused, and so is turning exclusivity on for a group whose labels already share an issue. */
  exclusive: Scalars['Boolean']['output'];
  id: Scalars['UUID']['output'];
  name: Scalars['String']['output'];
  updatedAt: Scalars['DateTime']['output'];
};

export type LabelGroupCreateInput = {
  exclusive?: InputMaybe<Scalars['Boolean']['input']>;
  name: Scalars['String']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type LabelGroupDeleteInput = {
  id: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type LabelGroupDeletePayload = {
  __typename?: 'LabelGroupDeletePayload';
  deletedGroupId?: Maybe<Scalars['UUID']['output']>;
  errors: Array<ValidationErrorType>;
};

export type LabelGroupPayload = {
  __typename?: 'LabelGroupPayload';
  errors: Array<ValidationErrorType>;
  group?: Maybe<LabelGroup>;
};

export type LabelGroupUpdateInput = {
  exclusive: Scalars['Boolean']['input'];
  id: Scalars['UUID']['input'];
  name: Scalars['String']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type LabelPayload = {
  __typename?: 'LabelPayload';
  errors: Array<ValidationErrorType>;
  label?: Maybe<Label>;
};

export type LabelSetGroupInput = {
  groupId?: InputMaybe<Scalars['UUID']['input']>;
  labelId: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type LabelUpdateInput = {
  color: Scalars['String']['input'];
  id: Scalars['UUID']['input'];
  name: Scalars['String']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type LoginInput = {
  email: Scalars['String']['input'];
  password: Scalars['String']['input'];
};

export type LoginPayload = {
  __typename?: 'LoginPayload';
  errors: Array<ValidationErrorType>;
  user?: Maybe<User>;
};

export type LogoutPayload = {
  __typename?: 'LogoutPayload';
  errors: Array<ValidationErrorType>;
  signedOut: Scalars['Boolean']['output'];
};

export type MemberRemoveInput = {
  userId: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type MemberRemovePayload = {
  __typename?: 'MemberRemovePayload';
  errors: Array<ValidationErrorType>;
  removedUserId?: Maybe<Scalars['UUID']['output']>;
};

export type MemberRoleUpdateInput = {
  role: WorkspaceRole;
  userId: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type Mutation = {
  __typename?: 'Mutation';
  commentCreate: CommentCreatePayload;
  commentDelete: CommentDeletePayload;
  cycleCreate: CyclePayload;
  cycleDelete: CycleDeletePayload;
  cycleUpdate: CyclePayload;
  documentCommentCreate: DocumentCommentPayload;
  documentCommentDelete: DocumentCommentDeletePayload;
  documentCreate: DocumentPayload;
  documentDelete: DocumentDeletePayload;
  documentEdit: DocumentPayload;
  documentRestore: DocumentPayload;
  embeddingsRefresh: Scalars['Int']['output'];
  environmentCreate: EnvironmentPayload;
  favoriteAdd: FavoritePayload;
  favoriteRemove: FavoriteDeletePayload;
  favoriteReorder: FavoritePayload;
  githubAutomationSet: GithubIntegrationPayload;
  githubDisconnect: GithubIntegration;
  githubRepositoriesSet: GithubIntegrationPayload;
  initiativeClearParent: InitiativePayload;
  initiativeCreate: InitiativePayload;
  initiativeDelete: InitiativeDeletePayload;
  initiativeProjectAdd: InitiativePayload;
  initiativeProjectRemove: InitiativePayload;
  initiativeSetParent: InitiativePayload;
  initiativeUpdate: InitiativePayload;
  initiativeUpdatePost: InitiativeUpdatePayload;
  /** Redeem an invitation token as the authenticated caller. */
  invitationAccept: InvitationAcceptPayload;
  /** Invite an email address to the workspace. Returns the raw invitation token once and never again. */
  invitationCreate: InvitationCreatePayload;
  /** Withdraw an invitation. Requires the admin or owner role. */
  invitationRevoke: InvitationRevokePayload;
  issueArchive: IssueArchivePayload;
  issueBulkArchive: IssueBulkPayload;
  issueBulkUpdate: IssueBulkPayload;
  issueClearParent: IssueParentPayload;
  issueCreate: IssueCreatePayload;
  issueCreateFromTemplate: IssueCreateFromTemplatePayload;
  issueLabelAttach: IssueLabelPayload;
  issueLabelDetach: IssueLabelPayload;
  issueRelationCreate: IssueRelationCreatePayload;
  issueRelationDelete: IssueRelationDeletePayload;
  issueSetCycle: IssueSetCyclePayload;
  issueSetParent: IssueParentPayload;
  issueSetProject: IssueSetProjectPayload;
  issueSubscribe: IssueSubscriptionPayload;
  issueTemplateCreate: IssueTemplateSavePayload;
  issueTemplateDelete: IssueTemplateDeletePayload;
  issueTemplateRecurrenceClear: IssueRecurrenceClearPayload;
  issueTemplateRecurrenceSet: IssueRecurrenceSetPayload;
  issueTemplateUpdate: IssueTemplateSavePayload;
  issueUnsubscribe: IssueSubscriptionPayload;
  issueUpdate: IssueUpdatePayload;
  labelCreate: LabelPayload;
  labelDelete: LabelDeletePayload;
  labelGroupCreate: LabelGroupPayload;
  labelGroupDelete: LabelGroupDeletePayload;
  labelGroupUpdate: LabelGroupPayload;
  labelSetGroup: LabelPayload;
  labelUpdate: LabelPayload;
  login: LoginPayload;
  logout: LogoutPayload;
  /** Remove someone from the workspace. Requires the admin or owner role, and cannot remove the last owner. */
  memberRemove: MemberRemovePayload;
  /** Change a member's role. Requires the admin or owner role. */
  memberRoleUpdate: WorkspaceMemberPayload;
  notificationMarkAllRead: NotificationMarkAllReadPayload;
  notificationMarkRead: NotificationMarkReadPayload;
  projectCreate: ProjectPayload;
  projectDelete: ProjectDeletePayload;
  projectDependencyAdd: ProjectDependencyPayload;
  projectDependencyRemove: ProjectDependencyPayload;
  projectMilestoneCreate: ProjectMilestonePayload;
  projectMilestoneDelete: ProjectMilestoneDeletePayload;
  projectMilestoneUpdate: ProjectMilestonePayload;
  projectTeamAdd: ProjectPayload;
  projectTeamRemove: ProjectPayload;
  projectUpdate: ProjectPayload;
  projectUpdatePost: ProjectUpdatePayload;
  register: RegisterPayload;
  releaseCreate: ReleasePayload;
  releaseDelete: ReleaseDeletePayload;
  releaseStatusSet: ReleasePayload;
  savedViewCreate: SavedViewPayload;
  savedViewDelete: SavedViewDeletePayload;
  savedViewUpdate: SavedViewPayload;
  slackChannelsSync: SlackChannelsSyncPayload;
  slackDefaultChannelSet: SlackNotificationSettingsPayload;
  slackDisconnect: SlackDisconnectPayload;
  slackNotificationPreferenceSet: SlackNotificationSettingsPayload;
  slackTestNotification: SlackTestNotificationPayload;
  /** Create a team, seeded with the default workflow states. Requires the admin or owner role. */
  teamCreate: TeamPayload;
  /** Choose what a team's estimates count -- points, hours, t-shirt sizes, or nothing named. Requires the admin or owner role. Issues already estimated keep their numbers; the scale bounds what may be written from now on. */
  teamEstimateScaleSet: TeamPayload;
  triageAccept: TriagePayload;
  triageChangeTeam: TriagePayload;
  triageDecline: TriagePayload;
  triageEnter: TriagePayload;
  triageMarkDuplicate: TriagePayload;
  /** Create a workspace. The authenticated caller becomes its owner. */
  workspaceCreate: WorkspacePayload;
};


export type MutationCommentCreateArgs = {
  input: CommentCreateInput;
};


export type MutationCommentDeleteArgs = {
  input: CommentDeleteInput;
};


export type MutationCycleCreateArgs = {
  input: CycleCreateInput;
};


export type MutationCycleDeleteArgs = {
  id: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};


export type MutationCycleUpdateArgs = {
  input: CycleUpdateInput;
};


export type MutationDocumentCommentCreateArgs = {
  input: DocumentCommentCreateInput;
};


export type MutationDocumentCommentDeleteArgs = {
  input: DocumentCommentDeleteInput;
};


export type MutationDocumentCreateArgs = {
  input: DocumentCreateInput;
};


export type MutationDocumentDeleteArgs = {
  input: DocumentDeleteInput;
};


export type MutationDocumentEditArgs = {
  input: DocumentEditInput;
};


export type MutationDocumentRestoreArgs = {
  input: DocumentRestoreInput;
};


export type MutationEmbeddingsRefreshArgs = {
  limit?: Scalars['Int']['input'];
  workspaceSlug: Scalars['String']['input'];
};


export type MutationEnvironmentCreateArgs = {
  input: EnvironmentCreateInput;
};


export type MutationFavoriteAddArgs = {
  input: FavoriteAddInput;
};


export type MutationFavoriteRemoveArgs = {
  input: FavoriteRemoveInput;
};


export type MutationFavoriteReorderArgs = {
  input: FavoriteReorderInput;
};


export type MutationGithubAutomationSetArgs = {
  input: GithubAutomationSetInput;
};


export type MutationGithubDisconnectArgs = {
  input: GithubDisconnectInput;
};


export type MutationGithubRepositoriesSetArgs = {
  input: GithubRepositoriesSetInput;
};


export type MutationInitiativeClearParentArgs = {
  input: InitiativeClearParentInput;
};


export type MutationInitiativeCreateArgs = {
  input: InitiativeCreateInput;
};


export type MutationInitiativeDeleteArgs = {
  input: InitiativeDeleteInput;
};


export type MutationInitiativeProjectAddArgs = {
  input: InitiativeProjectInput;
};


export type MutationInitiativeProjectRemoveArgs = {
  input: InitiativeProjectInput;
};


export type MutationInitiativeSetParentArgs = {
  input: InitiativeSetParentInput;
};


export type MutationInitiativeUpdateArgs = {
  input: InitiativeUpdateInput;
};


export type MutationInitiativeUpdatePostArgs = {
  input: InitiativeUpdatePostInput;
};


export type MutationInvitationAcceptArgs = {
  input: InvitationAcceptInput;
};


export type MutationInvitationCreateArgs = {
  input: InvitationCreateInput;
};


export type MutationInvitationRevokeArgs = {
  input: InvitationRevokeInput;
};


export type MutationIssueArchiveArgs = {
  id: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};


export type MutationIssueBulkArchiveArgs = {
  input: IssueBulkArchiveInput;
};


export type MutationIssueBulkUpdateArgs = {
  input: IssueBulkUpdateInput;
};


export type MutationIssueClearParentArgs = {
  input: IssueClearParentInput;
};


export type MutationIssueCreateArgs = {
  input: IssueCreateInput;
};


export type MutationIssueCreateFromTemplateArgs = {
  input: IssueCreateFromTemplateInput;
};


export type MutationIssueLabelAttachArgs = {
  input: IssueLabelInput;
};


export type MutationIssueLabelDetachArgs = {
  input: IssueLabelInput;
};


export type MutationIssueRelationCreateArgs = {
  input: IssueRelationCreateInput;
};


export type MutationIssueRelationDeleteArgs = {
  input: IssueRelationDeleteInput;
};


export type MutationIssueSetCycleArgs = {
  input: IssueSetCycleInput;
};


export type MutationIssueSetParentArgs = {
  input: IssueSetParentInput;
};


export type MutationIssueSetProjectArgs = {
  input: IssueSetProjectInput;
};


export type MutationIssueSubscribeArgs = {
  input: IssueSubscriptionInput;
};


export type MutationIssueTemplateCreateArgs = {
  input: IssueTemplateCreateInput;
};


export type MutationIssueTemplateDeleteArgs = {
  input: IssueTemplateDeleteInput;
};


export type MutationIssueTemplateRecurrenceClearArgs = {
  input: IssueRecurrenceClearInput;
};


export type MutationIssueTemplateRecurrenceSetArgs = {
  input: IssueRecurrenceSetInput;
};


export type MutationIssueTemplateUpdateArgs = {
  input: IssueTemplateUpdateInput;
};


export type MutationIssueUnsubscribeArgs = {
  input: IssueSubscriptionInput;
};


export type MutationIssueUpdateArgs = {
  id: Scalars['UUID']['input'];
  input: IssueUpdateInput;
};


export type MutationLabelCreateArgs = {
  input: LabelCreateInput;
};


export type MutationLabelDeleteArgs = {
  input: LabelDeleteInput;
};


export type MutationLabelGroupCreateArgs = {
  input: LabelGroupCreateInput;
};


export type MutationLabelGroupDeleteArgs = {
  input: LabelGroupDeleteInput;
};


export type MutationLabelGroupUpdateArgs = {
  input: LabelGroupUpdateInput;
};


export type MutationLabelSetGroupArgs = {
  input: LabelSetGroupInput;
};


export type MutationLabelUpdateArgs = {
  input: LabelUpdateInput;
};


export type MutationLoginArgs = {
  input: LoginInput;
};


export type MutationMemberRemoveArgs = {
  input: MemberRemoveInput;
};


export type MutationMemberRoleUpdateArgs = {
  input: MemberRoleUpdateInput;
};


export type MutationNotificationMarkAllReadArgs = {
  input: NotificationMarkAllReadInput;
};


export type MutationNotificationMarkReadArgs = {
  input: NotificationMarkReadInput;
};


export type MutationProjectCreateArgs = {
  input: ProjectCreateInput;
};


export type MutationProjectDeleteArgs = {
  input: ProjectDeleteInput;
};


export type MutationProjectDependencyAddArgs = {
  input: ProjectDependencyInput;
};


export type MutationProjectDependencyRemoveArgs = {
  input: ProjectDependencyInput;
};


export type MutationProjectMilestoneCreateArgs = {
  input: ProjectMilestoneCreateInput;
};


export type MutationProjectMilestoneDeleteArgs = {
  input: ProjectMilestoneDeleteInput;
};


export type MutationProjectMilestoneUpdateArgs = {
  input: ProjectMilestoneUpdateInput;
};


export type MutationProjectTeamAddArgs = {
  input: ProjectTeamInput;
};


export type MutationProjectTeamRemoveArgs = {
  input: ProjectTeamInput;
};


export type MutationProjectUpdateArgs = {
  input: ProjectUpdateInput;
};


export type MutationProjectUpdatePostArgs = {
  input: ProjectUpdatePostInput;
};


export type MutationRegisterArgs = {
  input: RegisterInput;
};


export type MutationReleaseCreateArgs = {
  input: ReleaseCreateInput;
};


export type MutationReleaseDeleteArgs = {
  input: ReleaseDeleteInput;
};


export type MutationReleaseStatusSetArgs = {
  input: ReleaseStatusSetInput;
};


export type MutationSavedViewCreateArgs = {
  input: SavedViewCreateInput;
};


export type MutationSavedViewDeleteArgs = {
  input: SavedViewDeleteInput;
};


export type MutationSavedViewUpdateArgs = {
  input: SavedViewUpdateInput;
};


export type MutationSlackChannelsSyncArgs = {
  input: SlackChannelsSyncInput;
};


export type MutationSlackDefaultChannelSetArgs = {
  input: SlackDefaultChannelSetInput;
};


export type MutationSlackDisconnectArgs = {
  input: SlackDisconnectInput;
};


export type MutationSlackNotificationPreferenceSetArgs = {
  input: SlackNotificationPreferenceSetInput;
};


export type MutationSlackTestNotificationArgs = {
  input: SlackTestNotificationInput;
};


export type MutationTeamCreateArgs = {
  input: TeamCreateInput;
};


export type MutationTeamEstimateScaleSetArgs = {
  input: TeamEstimateScaleSetInput;
};


export type MutationTriageAcceptArgs = {
  input: TriageAcceptInput;
};


export type MutationTriageChangeTeamArgs = {
  input: TriageChangeTeamInput;
};


export type MutationTriageDeclineArgs = {
  input: TriageDeclineInput;
};


export type MutationTriageEnterArgs = {
  input: TriageEnterInput;
};


export type MutationTriageMarkDuplicateArgs = {
  input: TriageMarkDuplicateInput;
};


export type MutationWorkspaceCreateArgs = {
  input: WorkspaceCreateInput;
};

export type Notification = {
  __typename?: 'Notification';
  actorId?: Maybe<Scalars['UUID']['output']>;
  createdAt: Scalars['DateTime']['output'];
  id: Scalars['UUID']['output'];
  /** The issue this is about, or null where it is no longer visible -- an archived issue, most often. Cheap to select: one query per page of notifications rather than one per row. */
  issue?: Maybe<IssueSummary>;
  issueId: Scalars['UUID']['output'];
  kind: NotificationKind;
  readAt?: Maybe<Scalars['DateTime']['output']>;
};

export type NotificationConnection = {
  __typename?: 'NotificationConnection';
  nodes: Array<Notification>;
  pageInfo: PageInfo;
};

export type NotificationKind =
  | 'ASSIGNED'
  | 'BLOCKED'
  | 'COMMENTED'
  | 'DUE_SOON'
  | 'STATUS_CHANGED';

export type NotificationMarkAllReadInput = {
  workspaceSlug: Scalars['String']['input'];
};

export type NotificationMarkAllReadPayload = {
  __typename?: 'NotificationMarkAllReadPayload';
  errors: Array<ValidationErrorType>;
  markedCount: Scalars['Int']['output'];
};

export type NotificationMarkReadInput = {
  id: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type NotificationMarkReadPayload = {
  __typename?: 'NotificationMarkReadPayload';
  errors: Array<ValidationErrorType>;
  notification?: Maybe<Notification>;
};

/** Which end of an ordering a list starts from. */
export type OrderDirection =
  | 'ASC'
  | 'DESC';

export type PageInfo = {
  __typename?: 'PageInfo';
  endCursor?: Maybe<Scalars['String']['output']>;
  hasNextPage: Scalars['Boolean']['output'];
};

/** Live, unfinished issues at one priority level. `priority` is the stored 0-4, where 0 means NO priority rather than the lowest one. Naming the levels is the client's job. */
export type PriorityCount = {
  __typename?: 'PriorityCount';
  issues: Scalars['Int']['output'];
  priority: Scalars['Int']['output'];
};

export type Project = {
  __typename?: 'Project';
  createdAt: Scalars['DateTime']['output'];
  dependencies: ProjectDependencies;
  description?: Maybe<Scalars['String']['output']>;
  health?: Maybe<Health>;
  id: Scalars['UUID']['output'];
  leadId?: Maybe<Scalars['UUID']['output']>;
  milestones: Array<ProjectMilestone>;
  name: Scalars['String']['output'];
  state: ProjectState;
  targetDate?: Maybe<Scalars['Date']['output']>;
  teamIds: Array<Scalars['UUID']['output']>;
  updatedAt: Scalars['DateTime']['output'];
  updates: Array<ProjectUpdate>;
};

export type ProjectConnection = {
  __typename?: 'ProjectConnection';
  nodes: Array<Project>;
  pageInfo: PageInfo;
};

export type ProjectCreateInput = {
  description?: InputMaybe<Scalars['String']['input']>;
  leadId?: InputMaybe<Scalars['UUID']['input']>;
  name: Scalars['String']['input'];
  state?: ProjectState;
  targetDate?: InputMaybe<Scalars['Date']['input']>;
  workspaceSlug: Scalars['String']['input'];
};

export type ProjectDeleteInput = {
  id: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type ProjectDeletePayload = {
  __typename?: 'ProjectDeletePayload';
  deletedProjectId?: Maybe<Scalars['UUID']['output']>;
  errors: Array<ValidationErrorType>;
};

export type ProjectDependencies = {
  __typename?: 'ProjectDependencies';
  blockedBy: Array<Scalars['UUID']['output']>;
  blocks: Array<Scalars['UUID']['output']>;
};

export type ProjectDependencyInput = {
  blockedProjectId: Scalars['UUID']['input'];
  blockingProjectId: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type ProjectDependencyPayload = {
  __typename?: 'ProjectDependencyPayload';
  dependencies?: Maybe<ProjectDependencies>;
  errors: Array<ValidationErrorType>;
};

export type ProjectMilestone = {
  __typename?: 'ProjectMilestone';
  createdAt: Scalars['DateTime']['output'];
  id: Scalars['UUID']['output'];
  name: Scalars['String']['output'];
  position: Scalars['Int']['output'];
  projectId: Scalars['UUID']['output'];
  targetDate?: Maybe<Scalars['Date']['output']>;
  updatedAt: Scalars['DateTime']['output'];
};

export type ProjectMilestoneCreateInput = {
  name: Scalars['String']['input'];
  projectId: Scalars['UUID']['input'];
  targetDate?: InputMaybe<Scalars['Date']['input']>;
  workspaceSlug: Scalars['String']['input'];
};

export type ProjectMilestoneDeleteInput = {
  id: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type ProjectMilestoneDeletePayload = {
  __typename?: 'ProjectMilestoneDeletePayload';
  deletedMilestoneId?: Maybe<Scalars['UUID']['output']>;
  errors: Array<ValidationErrorType>;
};

export type ProjectMilestonePayload = {
  __typename?: 'ProjectMilestonePayload';
  errors: Array<ValidationErrorType>;
  milestone?: Maybe<ProjectMilestone>;
};

export type ProjectMilestoneUpdateInput = {
  id: Scalars['UUID']['input'];
  name?: InputMaybe<Scalars['String']['input']>;
  position?: InputMaybe<Scalars['Int']['input']>;
  targetDate?: InputMaybe<Scalars['Date']['input']>;
  workspaceSlug: Scalars['String']['input'];
};

export type ProjectPayload = {
  __typename?: 'ProjectPayload';
  errors: Array<ValidationErrorType>;
  project?: Maybe<Project>;
};

/** One project's live issues and how many are done. Two counts rather than a percentage: 2 of 5 and 200 of 500 are the same fraction and are not equally worth acting on. Both exclude archived issues, so filing work away cannot make progress fall. */
export type ProjectProgress = {
  __typename?: 'ProjectProgress';
  completed: Scalars['Int']['output'];
  issues: Scalars['Int']['output'];
  name: Scalars['String']['output'];
  projectId: Scalars['UUID']['output'];
  /** The project's own state: planned, started, paused, completed or canceled. Independent of its issues -- a project can be marked completed with issues still open, and the screen shows both. */
  state: Scalars['String']['output'];
};

export type ProjectState =
  | 'CANCELED'
  | 'COMPLETED'
  | 'PAUSED'
  | 'PLANNED'
  | 'STARTED';

export type ProjectTeamInput = {
  projectId: Scalars['UUID']['input'];
  teamId: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type ProjectUpdate = {
  __typename?: 'ProjectUpdate';
  authorId: Scalars['UUID']['output'];
  body: Scalars['String']['output'];
  createdAt: Scalars['DateTime']['output'];
  health: Health;
  id: Scalars['UUID']['output'];
  projectId: Scalars['UUID']['output'];
};

export type ProjectUpdateInput = {
  description?: InputMaybe<Scalars['String']['input']>;
  id: Scalars['UUID']['input'];
  leadId?: InputMaybe<Scalars['UUID']['input']>;
  name?: InputMaybe<Scalars['String']['input']>;
  state?: InputMaybe<ProjectState>;
  targetDate?: InputMaybe<Scalars['Date']['input']>;
  workspaceSlug: Scalars['String']['input'];
};

export type ProjectUpdatePayload = {
  __typename?: 'ProjectUpdatePayload';
  errors: Array<ValidationErrorType>;
  update?: Maybe<ProjectUpdate>;
};

export type ProjectUpdatePostInput = {
  body: Scalars['String']['input'];
  health: Health;
  projectId: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type Query = {
  __typename?: 'Query';
  cycle?: Maybe<Cycle>;
  cycles: Array<Cycle>;
  document?: Maybe<Document>;
  documents: DocumentConnection;
  embeddingIndexingState: EmbeddingIndexingState;
  environments: Array<Environment>;
  /** This viewer's favorites in this workspace, in their own order. Per-user and per-workspace: the same person in two workspaces has two independent lists. */
  favorites: Array<Favorite>;
  githubIntegration: GithubIntegration;
  initiative?: Maybe<Initiative>;
  initiatives: InitiativeConnection;
  /** Invitations to this workspace that have not been accepted or expired. Admins and owners only. */
  invitations: Array<WorkspaceInvitation>;
  issue?: Maybe<Issue>;
  issueDuplicateSuggestions: Array<DuplicateSuggestion>;
  issueSubscribers: Array<IssueSubscriber>;
  issueTemplate?: Maybe<IssueTemplate>;
  issueTemplates: Array<IssueTemplate>;
  issueViewerIsSubscribed: Scalars['Boolean']['output'];
  issues: IssueConnection;
  label?: Maybe<Label>;
  labelGroup?: Maybe<LabelGroup>;
  labelGroups: Array<LabelGroup>;
  labels: LabelConnection;
  me?: Maybe<User>;
  myWorkspace: WorkspaceMembership;
  myWorkspaces: Array<WorkspaceMembership>;
  notificationUnreadCount: Scalars['Int']['output'];
  notifications: NotificationConnection;
  project?: Maybe<Project>;
  projects: ProjectConnection;
  release?: Maybe<Release>;
  releases: ReleaseConnection;
  savedView?: Maybe<SavedView>;
  savedViews: SavedViewConnection;
  search: SearchResults;
  slackChannels: Array<SlackChannel>;
  slackIntegration: SlackIntegration;
  slackNotificationSettings: SlackNotificationSettings;
  /** The teams in a workspace, each with the workflow states its issues can occupy. */
  teams: Array<Team>;
  triageCount: Scalars['Int']['output'];
  triageIssues: TriageIssueConnection;
  /** How this workspace has been moving over the last `days` days, which must be between 1 and 180: a larger window is refused rather than quietly shortened. One aggregate rather than a field per metric, because they share a window and a tenant and are rendered together -- separate root fields would let one document ask for six different windows at once. */
  workspaceAnalytics: WorkspaceAnalytics;
  /** Everyone in a workspace, with the role each holds, including people who have left -- see `removedAt`. Members only. */
  workspaceMembers: Array<WorkspaceMember>;
};


export type QueryCycleArgs = {
  id: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};


export type QueryCyclesArgs = {
  teamId: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};


export type QueryDocumentArgs = {
  id: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};


export type QueryDocumentsArgs = {
  after?: InputMaybe<Scalars['String']['input']>;
  first?: Scalars['Int']['input'];
  initiativeId?: InputMaybe<Scalars['UUID']['input']>;
  projectId?: InputMaybe<Scalars['UUID']['input']>;
  workspaceSlug: Scalars['String']['input'];
};


export type QueryEmbeddingIndexingStateArgs = {
  workspaceSlug: Scalars['String']['input'];
};


export type QueryEnvironmentsArgs = {
  workspaceSlug: Scalars['String']['input'];
};


export type QueryFavoritesArgs = {
  workspaceSlug: Scalars['String']['input'];
};


export type QueryGithubIntegrationArgs = {
  workspaceSlug: Scalars['String']['input'];
};


export type QueryInitiativeArgs = {
  id: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};


export type QueryInitiativesArgs = {
  after?: InputMaybe<Scalars['String']['input']>;
  first?: Scalars['Int']['input'];
  workspaceSlug: Scalars['String']['input'];
};


export type QueryInvitationsArgs = {
  workspaceSlug: Scalars['String']['input'];
};


export type QueryIssueArgs = {
  id: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};


export type QueryIssueDuplicateSuggestionsArgs = {
  description?: InputMaybe<Scalars['String']['input']>;
  excludeIssueId?: InputMaybe<Scalars['UUID']['input']>;
  first?: Scalars['Int']['input'];
  title: Scalars['String']['input'];
  workspaceSlug: Scalars['String']['input'];
};


export type QueryIssueSubscribersArgs = {
  issueId: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};


export type QueryIssueTemplateArgs = {
  id: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};


export type QueryIssueTemplatesArgs = {
  teamId?: InputMaybe<Scalars['UUID']['input']>;
  workspaceSlug: Scalars['String']['input'];
};


export type QueryIssueViewerIsSubscribedArgs = {
  issueId: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};


export type QueryIssuesArgs = {
  after?: InputMaybe<Scalars['String']['input']>;
  filter?: InputMaybe<IssueFilterInput>;
  first?: Scalars['Int']['input'];
  orderBy?: InputMaybe<IssueOrderInput>;
  workspaceSlug: Scalars['String']['input'];
};


export type QueryLabelArgs = {
  id: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};


export type QueryLabelGroupArgs = {
  id: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};


export type QueryLabelGroupsArgs = {
  workspaceSlug: Scalars['String']['input'];
};


export type QueryLabelsArgs = {
  after?: InputMaybe<Scalars['String']['input']>;
  first?: Scalars['Int']['input'];
  workspaceSlug: Scalars['String']['input'];
};


export type QueryMyWorkspaceArgs = {
  slug: Scalars['String']['input'];
};


export type QueryNotificationUnreadCountArgs = {
  workspaceSlug: Scalars['String']['input'];
};


export type QueryNotificationsArgs = {
  after?: InputMaybe<Scalars['String']['input']>;
  first?: Scalars['Int']['input'];
  unreadOnly?: Scalars['Boolean']['input'];
  workspaceSlug: Scalars['String']['input'];
};


export type QueryProjectArgs = {
  id: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};


export type QueryProjectsArgs = {
  after?: InputMaybe<Scalars['String']['input']>;
  first?: Scalars['Int']['input'];
  workspaceSlug: Scalars['String']['input'];
};


export type QueryReleaseArgs = {
  id: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};


export type QueryReleasesArgs = {
  after?: InputMaybe<Scalars['String']['input']>;
  first?: Scalars['Int']['input'];
  workspaceSlug: Scalars['String']['input'];
};


export type QuerySavedViewArgs = {
  id: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};


export type QuerySavedViewsArgs = {
  after?: InputMaybe<Scalars['String']['input']>;
  first?: Scalars['Int']['input'];
  teamId?: InputMaybe<Scalars['UUID']['input']>;
  workspaceSlug: Scalars['String']['input'];
};


export type QuerySearchArgs = {
  first?: Scalars['Int']['input'];
  query: Scalars['String']['input'];
  workspaceSlug: Scalars['String']['input'];
};


export type QuerySlackChannelsArgs = {
  workspaceSlug: Scalars['String']['input'];
};


export type QuerySlackIntegrationArgs = {
  workspaceSlug: Scalars['String']['input'];
};


export type QuerySlackNotificationSettingsArgs = {
  workspaceSlug: Scalars['String']['input'];
};


export type QueryTeamsArgs = {
  workspaceSlug: Scalars['String']['input'];
};


export type QueryTriageCountArgs = {
  teamId: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};


export type QueryTriageIssuesArgs = {
  after?: InputMaybe<Scalars['String']['input']>;
  first?: Scalars['Int']['input'];
  teamId: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};


export type QueryWorkspaceAnalyticsArgs = {
  days?: Scalars['Int']['input'];
  workspaceSlug: Scalars['String']['input'];
};


export type QueryWorkspaceMembersArgs = {
  workspaceSlug: Scalars['String']['input'];
};

/** How often a template files itself. Deliberately three words and not a cron expression: a cron field is a small language with its own parser and its own surprises, bought so a project tracker can express a schedule nobody asks a project tracker for. */
export type RecurrenceFrequency =
  | 'DAILY'
  | 'MONTHLY'
  | 'WEEKLY';

export type RegisterInput = {
  email: Scalars['String']['input'];
  name?: InputMaybe<Scalars['String']['input']>;
  password: Scalars['String']['input'];
};

export type RegisterPayload = {
  __typename?: 'RegisterPayload';
  errors: Array<ValidationErrorType>;
  user?: Maybe<User>;
};

export type Release = {
  __typename?: 'Release';
  commitSha: Scalars['String']['output'];
  createdAt: Scalars['DateTime']['output'];
  deployedAt?: Maybe<Scalars['DateTime']['output']>;
  environmentId: Scalars['UUID']['output'];
  id: Scalars['UUID']['output'];
  issueIds: Array<Scalars['UUID']['output']>;
  name: Scalars['String']['output'];
  notes: Scalars['String']['output'];
  previousCommitSha?: Maybe<Scalars['String']['output']>;
  pullRequestNumbers: Array<Scalars['Int']['output']>;
  repositoryId: Scalars['ID']['output'];
  status: ReleaseStatus;
  updatedAt: Scalars['DateTime']['output'];
};

export type ReleaseConnection = {
  __typename?: 'ReleaseConnection';
  nodes: Array<Release>;
  pageInfo: PageInfo;
};

export type ReleaseCreateInput = {
  commitSha: Scalars['String']['input'];
  environmentId: Scalars['UUID']['input'];
  name: Scalars['String']['input'];
  previousCommitSha?: InputMaybe<Scalars['String']['input']>;
  repositoryId: Scalars['ID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type ReleaseDeleteInput = {
  id: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type ReleaseDeletePayload = {
  __typename?: 'ReleaseDeletePayload';
  deletedReleaseId?: Maybe<Scalars['UUID']['output']>;
  errors: Array<ValidationErrorType>;
};

export type ReleasePayload = {
  __typename?: 'ReleasePayload';
  errors: Array<ValidationErrorType>;
  release?: Maybe<Release>;
};

export type ReleaseStatus =
  | 'DEPLOYED'
  | 'FAILED'
  | 'PENDING'
  | 'ROLLED_BACK';

export type ReleaseStatusSetInput = {
  id: Scalars['UUID']['input'];
  status: ReleaseStatus;
  workspaceSlug: Scalars['String']['input'];
};

export type SavedView = {
  __typename?: 'SavedView';
  createdAt: Scalars['DateTime']['output'];
  createdBy: Scalars['UUID']['output'];
  filter: SavedViewFilter;
  grouping?: Maybe<SavedViewGrouping>;
  id: Scalars['UUID']['output'];
  /** The issues this view selects, in the order it stores. Loading a saved view is this field: the filter and the ordering come from the stored row, never from the document, so the page is the one that was saved. */
  issues: IssueConnection;
  layout: SavedViewLayout;
  name: Scalars['String']['output'];
  orderDirection: OrderDirection;
  orderField: IssueOrderField;
  subgrouping?: Maybe<SavedViewGrouping>;
  teamId?: Maybe<Scalars['UUID']['output']>;
  updatedAt: Scalars['DateTime']['output'];
  visibility: SavedViewVisibility;
};


export type SavedViewIssuesArgs = {
  after?: InputMaybe<Scalars['String']['input']>;
  first?: Scalars['Int']['input'];
};

export type SavedViewConnection = {
  __typename?: 'SavedViewConnection';
  nodes: Array<SavedView>;
  pageInfo: PageInfo;
};

export type SavedViewCreateInput = {
  filter?: InputMaybe<IssueFilterInput>;
  grouping?: InputMaybe<SavedViewGrouping>;
  layout?: SavedViewLayout;
  name: Scalars['String']['input'];
  orderBy?: InputMaybe<IssueOrderInput>;
  subgrouping?: InputMaybe<SavedViewGrouping>;
  teamId?: InputMaybe<Scalars['UUID']['input']>;
  visibility?: SavedViewVisibility;
  workspaceSlug: Scalars['String']['input'];
};

export type SavedViewDeleteInput = {
  id: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type SavedViewDeletePayload = {
  __typename?: 'SavedViewDeletePayload';
  deletedSavedViewId?: Maybe<Scalars['UUID']['output']>;
  errors: Array<ValidationErrorType>;
};

/** The filter a saved view stores, in the same vocabulary `IssueFilterInput` takes. Every field is a narrowing and none of them can widen a list beyond the workspace the request was authorized for. */
export type SavedViewFilter = {
  __typename?: 'SavedViewFilter';
  assignee?: Maybe<SavedViewIdFilter>;
  cycle?: Maybe<SavedViewIdFilter>;
  labelId?: Maybe<Scalars['UUID']['output']>;
  priority?: Maybe<Scalars['Int']['output']>;
  project?: Maybe<SavedViewIdFilter>;
  stateCategory?: Maybe<WorkflowStateCategory>;
  teamId?: Maybe<Scalars['UUID']['output']>;
  workflowStateId?: Maybe<Scalars['UUID']['output']>;
};

/** What a saved view gathers its rows by. The server stores and validates this choice; the grouping itself is a rendering of a page the client already holds. There is deliberately no LABEL: an issue wears many labels, so grouping by one would put the same issue in several groups and every count drawn from them would overstate the list. */
export type SavedViewGrouping =
  | 'ASSIGNEE'
  | 'CYCLE'
  | 'PRIORITY'
  | 'PROJECT'
  | 'TEAM'
  | 'WORKFLOW_STATE';

/** A filter on a column that may hold nothing. The wrapper's presence is the filter and its `id` is what to match: `{id: null}` selects the rows holding nothing -- unassigned, in no project, in no cycle -- while the wrapper itself being null means the view does not filter on that column at all. */
export type SavedViewIdFilter = {
  __typename?: 'SavedViewIdFilter';
  id?: Maybe<Scalars['UUID']['output']>;
};

/** How a saved view arranges the issues it selects. */
export type SavedViewLayout =
  | 'BOARD'
  | 'LIST';

export type SavedViewPayload = {
  __typename?: 'SavedViewPayload';
  errors: Array<ValidationErrorType>;
  savedView?: Maybe<SavedView>;
};

export type SavedViewUpdateInput = {
  filter?: InputMaybe<IssueFilterInput>;
  grouping?: InputMaybe<SavedViewGrouping>;
  id: Scalars['UUID']['input'];
  layout?: InputMaybe<SavedViewLayout>;
  name?: InputMaybe<Scalars['String']['input']>;
  orderBy?: InputMaybe<IssueOrderInput>;
  subgrouping?: InputMaybe<SavedViewGrouping>;
  teamId?: InputMaybe<Scalars['UUID']['input']>;
  visibility?: InputMaybe<SavedViewVisibility>;
  workspaceSlug: Scalars['String']['input'];
};

/** Who can see a saved view. PERSONAL is its creator alone; SHARED is every member of the workspace. Sharing is an update of this field rather than an operation of its own. */
export type SavedViewVisibility =
  | 'PERSONAL'
  | 'SHARED';

export type SearchResults = {
  __typename?: 'SearchResults';
  issues: Array<Issue>;
  projects: Array<Project>;
};

export type SlackChannel = {
  __typename?: 'SlackChannel';
  id: Scalars['String']['output'];
  isAccessible: Scalars['Boolean']['output'];
  isArchived: Scalars['Boolean']['output'];
  isMember: Scalars['Boolean']['output'];
  isPrivate: Scalars['Boolean']['output'];
  name: Scalars['String']['output'];
};

export type SlackChannelsSyncInput = {
  workspaceSlug: Scalars['String']['input'];
};

export type SlackChannelsSyncPayload = {
  __typename?: 'SlackChannelsSyncPayload';
  channels: Array<SlackChannel>;
  failure?: Maybe<SlackFailure>;
};

export type SlackDefaultChannelSetInput = {
  channelId: Scalars['String']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type SlackDisconnectInput = {
  workspaceSlug: Scalars['String']['input'];
};

export type SlackDisconnectPayload = {
  __typename?: 'SlackDisconnectPayload';
  integration: SlackIntegration;
};

export type SlackFailure =
  | 'CHANNEL_UNAVAILABLE'
  | 'MISSING_SCOPE'
  | 'NOT_CONNECTED'
  | 'NO_DEFAULT_CHANNEL'
  | 'SLACK_REFUSED'
  | 'SLACK_UNREACHABLE';

export type SlackIntegration = {
  __typename?: 'SlackIntegration';
  scopes: Array<Scalars['String']['output']>;
  status: SlackIntegrationStatus;
  teamName?: Maybe<Scalars['String']['output']>;
};

export type SlackIntegrationStatus =
  | 'CONNECTED'
  | 'DISCONNECTED'
  | 'UNCONFIGURED';

export type SlackNotificationEvent =
  | 'ISSUE_ASSIGNED'
  | 'ISSUE_COMPLETED'
  | 'ISSUE_PRIORITY_URGENT'
  | 'PROJECT_HEALTH_CHANGED'
  | 'PROJECT_UPDATE_PUBLISHED'
  | 'PULL_REQUEST_MERGED';

export type SlackNotificationPreference = {
  __typename?: 'SlackNotificationPreference';
  enabled: Scalars['Boolean']['output'];
  event: SlackNotificationEvent;
};

export type SlackNotificationPreferenceSetInput = {
  enabled: Scalars['Boolean']['input'];
  event: SlackNotificationEvent;
  workspaceSlug: Scalars['String']['input'];
};

export type SlackNotificationSettings = {
  __typename?: 'SlackNotificationSettings';
  defaultChannelId?: Maybe<Scalars['String']['output']>;
  defaultChannelName?: Maybe<Scalars['String']['output']>;
  preferences: Array<SlackNotificationPreference>;
};

export type SlackNotificationSettingsPayload = {
  __typename?: 'SlackNotificationSettingsPayload';
  errors: Array<ValidationErrorType>;
  settings?: Maybe<SlackNotificationSettings>;
};

export type SlackTestNotificationInput = {
  workspaceSlug: Scalars['String']['input'];
};

export type SlackTestNotificationPayload = {
  __typename?: 'SlackTestNotificationPayload';
  delivered: Scalars['Boolean']['output'];
  failure?: Maybe<SlackFailure>;
};

/** Live issues sitting in one workflow-state category, as of now. A snapshot with no time dimension: the historical version needs a complete state history, which begins only at the activity migration. */
export type StateCategoryCount = {
  __typename?: 'StateCategoryCount';
  category: WorkflowStateCategory;
  /** Unarchived issues in this category. COMPLETED and CANCELED are two of the five buckets, not an excluded remainder -- finished work stays on the board until it is archived. */
  issues: Scalars['Int']['output'];
};

export type Team = {
  __typename?: 'Team';
  createdAt: Scalars['DateTime']['output'];
  /** The unit this team's estimates are in. Read it to LABEL an `Issue.estimate` -- the number alone says nothing, which is what this field exists to fix -- and to decide which values an estimate input may offer. An issue's scale is its team's; there is no per-issue override. */
  estimateScale: EstimateScale;
  id: Scalars['UUID']['output'];
  /** The prefix of this team's issue identifiers -- the ENG in ENG-42. Unique within the workspace, and not beyond it. */
  key: Scalars['String']['output'];
  name: Scalars['String']['output'];
  workflowStates: Array<WorkflowState>;
};

/** What one team delivered in this window, in that team's own estimate unit. There is deliberately no workspace-wide estimate total: a workspace whose teams estimate in points, hours and t-shirt sizes has no unit to sum them into, and t-shirt sizes are a ladder rather than a quantity at all. */
export type TeamCompletion = {
  __typename?: 'TeamCompletion';
  completed: Scalars['Int']['output'];
  /** The unit this team's estimates are in, and the reason there is no workspace-wide total to compare it against. */
  estimateScale: EstimateScale;
  /** The sum of those estimates, in this team's unit. Null when the team estimates in t-shirt sizes -- the stored integer is a position on a five-rung ladder, so summing two of them produces no size -- and also null when nothing was estimated. Read `estimated` to tell the two apart. */
  estimateTotal?: Maybe<Scalars['Int']['output']>;
  /** How many of those completions carried an estimate at all. */
  estimated: Scalars['Int']['output'];
  key: Scalars['String']['output'];
  name: Scalars['String']['output'];
  teamId: Scalars['UUID']['output'];
};

export type TeamCreateInput = {
  /** The prefix of this team's issue identifiers -- the ENG in ENG-42. 1-10 uppercase letters and digits, starting with a letter. Unique within the workspace. */
  key: Scalars['String']['input'];
  name: Scalars['String']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type TeamEstimateScaleSetInput = {
  scale: EstimateScale;
  teamId: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type TeamPayload = {
  __typename?: 'TeamPayload';
  errors: Array<ValidationErrorType>;
  team?: Maybe<Team>;
};

/** One UTC calendar day, and the work that stopped on it. Days are UTC for every viewer -- nothing in this schema records a person's timezone -- so a completion late in the evening west of Greenwich lands on the following day's point. */
export type ThroughputDay = {
  __typename?: 'ThroughputDay';
  /** Issues that reached a CANCELED state on this day. Reported separately and never added to `completed`: `issues.completed_at` is stamped for both terminal categories, so a single closed count would report abandonment as delivery. */
  canceled: Scalars['Int']['output'];
  /** Issues that reached a workflow state in the COMPLETED category on this day. */
  completed: Scalars['Int']['output'];
  /** Issues FILED on this day. From `created_at`, so it counts a different event from the other two and an issue can appear on one day here and another day there. */
  created: Scalars['Int']['output'];
  day: Scalars['Date']['output'];
};

/** The window's three series added up, and the completion rate derived from two of them. Summed from the same points the chart draws, so a headline can never disagree with the series under it. */
export type ThroughputTotals = {
  __typename?: 'ThroughputTotals';
  canceled: Scalars['Int']['output'];
  completed: Scalars['Int']['output'];
  /** completed / (completed + canceled): of the work that STOPPED in this window, the share that was delivered rather than abandoned. Deliberately NOT completed/created -- the issues finished this month are mostly not the ones filed this month, so that quotient can exceed 1 and is not a proportion of anything. Null when nothing stopped; 0 would report a failure that did not happen. */
  completionRate?: Maybe<Scalars['Float']['output']>;
  created: Scalars['Int']['output'];
};

export type TriageAcceptInput = {
  issueId: Scalars['UUID']['input'];
  workflowStateId: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type TriageChangeTeamInput = {
  issueId: Scalars['UUID']['input'];
  teamId: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type TriageDeclineInput = {
  issueId: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type TriageEnterInput = {
  issueId: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type TriageIssue = {
  __typename?: 'TriageIssue';
  /** When this issue entered the queue. The queue is ordered by it, oldest first, so this is also the issue's position in the list. */
  enteredAt: Scalars['DateTime']['output'];
  issue: IssueSummary;
};

export type TriageIssueConnection = {
  __typename?: 'TriageIssueConnection';
  nodes: Array<TriageIssue>;
  pageInfo: PageInfo;
};

export type TriageMarkDuplicateInput = {
  duplicateOfId: Scalars['UUID']['input'];
  issueId: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type TriagePayload = {
  __typename?: 'TriagePayload';
  errors: Array<ValidationErrorType>;
  issue?: Maybe<Issue>;
};

export type User = {
  __typename?: 'User';
  createdAt: Scalars['DateTime']['output'];
  email: Scalars['String']['output'];
  id: Scalars['UUID']['output'];
  name?: Maybe<Scalars['String']['output']>;
  updatedAt: Scalars['DateTime']['output'];
};

export type ValidationErrorType = {
  __typename?: 'ValidationErrorType';
  code: Scalars['String']['output'];
  field: Scalars['String']['output'];
  message: Scalars['String']['output'];
};

/** A status an issue can occupy on one team's board. */
export type WorkflowState = {
  __typename?: 'WorkflowState';
  /** The fixed category this state belongs to. Branch on this, never on the name, which the team owns and may change. */
  category: WorkflowStateCategory;
  color?: Maybe<Scalars['String']['output']>;
  id: Scalars['UUID']['output'];
  name: Scalars['String']['output'];
  position: Scalars['Int']['output'];
};

/** What a workflow state means, independent of what it is called. */
export type WorkflowStateCategory =
  | 'BACKLOG'
  | 'CANCELED'
  | 'COMPLETED'
  | 'STARTED'
  | 'UNSTARTED';

export type Workspace = {
  __typename?: 'Workspace';
  id: Scalars['UUID']['output'];
  name: Scalars['String']['output'];
  slug: Scalars['String']['output'];
};

/** How one workspace has been moving over a bounded recent window. Every figure is computed from stored rows at request time; nothing here is precomputed, and nothing here is estimated. Metrics this schema cannot compute honestly -- in-progress time, cycle burndown, and open counts over time -- are absent rather than approximated. */
export type WorkspaceAnalytics = {
  __typename?: 'WorkspaceAnalytics';
  /** How many distinct assignees hold unfinished work, including the unassigned pile as one. `workload` is a prefix of these. */
  assigneeTotal: Scalars['Int']['output'];
  /** Start of work to completion, with its coverage. Partial by construction; read `measured` against `completedTotal`. */
  cycleTime?: Maybe<CycleTimeSummary>;
  /** How many cycles overlap the window; `cycles` may be a prefix. */
  cycleTotal: Scalars['Int']['output'];
  /** Cycles whose span overlaps the window, newest first, bounded server-side. Each row's counts are over the whole cycle. */
  cycles: Array<CycleProgress>;
  /** The window length actually used, which is the one that was asked for: a value above 180 is refused rather than quietly reduced. */
  days: Scalars['Int']['output'];
  /** How long the UNFINISHED work has been open, as of now. A snapshot of the backlog, not of the window. Null when nothing is open. */
  issueAge?: Maybe<DurationSummary>;
  /** Creation to completion, over work delivered in this window. Complete -- both instants are columns on the issue -- so this is the duration to quote when `cycleTime.measured` is small. Null when nothing was delivered. */
  leadTime?: Maybe<DurationSummary>;
  /** Live, unfinished issues whose due date is before today. A thing due today is not yet late. */
  overdue: Scalars['Int']['output'];
  /** Live UNFINISHED issues by priority -- a different population from `stateMix`, and the same one as `workload`, because the question is what is on the plate now. Levels holding no issues are omitted. At most five rows: the column is constrained 0-4. */
  priorityMix: Array<PriorityCount>;
  /** How many projects hold live issues; `projects` may be a prefix. */
  projectTotal: Scalars['Int']['output'];
  /** Projects holding live issues, largest first, bounded server-side. Issues in no project are excluded rather than bucketed: most issues are in none, so that bar would be the biggest on every chart and would say nothing. */
  projects: Array<ProjectProgress>;
  /** The last day of the window, inclusive. Today, in UTC. */
  rangeEnd: Scalars['Date']['output'];
  rangeStart: Scalars['Date']['output'];
  /** A snapshot of now, not of the window. Every live issue, so COMPLETED and CANCELED are two of the buckets. Categories holding no issues are omitted. */
  stateMix: Array<StateCategoryCount>;
  /** How many teams delivered something; `teams` may be a prefix. */
  teamTotal: Scalars['Int']['output'];
  /** Teams that delivered something in the window, busiest first. */
  teams: Array<TeamCompletion>;
  /** One point per day in the window, including days on which nothing happened. A day with a zero is a fact; a day missing from a series is a hole a renderer will draw a line through. */
  throughput: Array<ThroughputDay>;
  /** `throughput` summed, and the completion rate it supports. */
  totals: ThroughputTotals;
  /** The busiest assignees, most work first, bounded server-side. Compare its length against `assigneeTotal` before describing it as the whole workspace. */
  workload: Array<AssigneeWorkload>;
};

export type WorkspaceCreateInput = {
  name: Scalars['String']['input'];
  /** The workspace's URL segment. Lowercase letters, digits and hyphens; must start and end with a letter or digit. */
  slug: Scalars['String']['input'];
};

/** An outstanding invitation to join a workspace. */
export type WorkspaceInvitation = {
  __typename?: 'WorkspaceInvitation';
  createdAt: Scalars['DateTime']['output'];
  email: Scalars['String']['output'];
  expiresAt: Scalars['DateTime']['output'];
  id: Scalars['UUID']['output'];
  role: WorkspaceRole;
};

/** One person in a workspace, and the role they hold there. */
export type WorkspaceMember = {
  __typename?: 'WorkspaceMember';
  createdAt: Scalars['DateTime']['output'];
  email: Scalars['String']['output'];
  name?: Maybe<Scalars['String']['output']>;
  /** When this person left the workspace, or null if they are still in it. A member list includes people who have left, so that anything they wrote still renders with their name; anything offering a choice of person -- an assignee picker, a lead -- must exclude the ones this field is set on. */
  removedAt?: Maybe<Scalars['DateTime']['output']>;
  role: WorkspaceRole;
  userId: Scalars['UUID']['output'];
};

export type WorkspaceMemberPayload = {
  __typename?: 'WorkspaceMemberPayload';
  errors: Array<ValidationErrorType>;
  member?: Maybe<WorkspaceMember>;
};

export type WorkspaceMembership = {
  __typename?: 'WorkspaceMembership';
  createdAt: Scalars['DateTime']['output'];
  role: WorkspaceRole;
  workspace: Workspace;
};

export type WorkspacePayload = {
  __typename?: 'WorkspacePayload';
  errors: Array<ValidationErrorType>;
  workspace?: Maybe<Workspace>;
};

export type WorkspaceRole =
  | 'ADMIN'
  | 'MEMBER'
  | 'OWNER';
