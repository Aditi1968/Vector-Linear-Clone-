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
  UUID: { input: string; output: string; }
};

export type Issue = {
  __typename?: 'Issue';
  /** When the issue was taken off the board. Always null here, because archived issues are absent from every query -- only the archive mutation's own result carries a value. */
  archivedAt?: Maybe<Scalars['DateTime']['output']>;
  assigneeId?: Maybe<Scalars['UUID']['output']>;
  /** When the issue stopped being worked on. Derived from the workflow state's category and not settable directly: it is non-null exactly while the issue sits in a completed or canceled state. */
  completedAt?: Maybe<Scalars['DateTime']['output']>;
  createdAt: Scalars['DateTime']['output'];
  /** Who filed the issue, or null where that is not known -- an issue created before accounts existed, or one whose author's account has since been deleted. */
  creatorId?: Maybe<Scalars['UUID']['output']>;
  description?: Maybe<Scalars['String']['output']>;
  /** A calendar day, not an instant: the same day for every viewer, in every timezone. */
  dueDate?: Maybe<Scalars['Date']['output']>;
  estimate?: Maybe<Scalars['Int']['output']>;
  id: Scalars['UUID']['output'];
  /** The name this issue is known by outside the product -- ENG-42. Its team's key, a hyphen, and the issue's number. */
  identifier: Scalars['String']['output'];
  /** Sequential within the team and never reused. Unique only alongside the team; two teams both have a number 42. */
  number: Scalars['Int']['output'];
  priority: Scalars['Int']['output'];
  teamId: Scalars['UUID']['output'];
  title: Scalars['String']['output'];
  updatedAt: Scalars['DateTime']['output'];
  workflowStateId: Scalars['UUID']['output'];
};

export type IssueArchivePayload = {
  __typename?: 'IssueArchivePayload';
  errors: Array<ValidationErrorType>;
  issue?: Maybe<Issue>;
};

export type IssueConnection = {
  __typename?: 'IssueConnection';
  nodes: Array<Issue>;
  pageInfo: PageInfo;
};

export type IssueCreateInput = {
  assigneeId?: InputMaybe<Scalars['UUID']['input']>;
  description?: InputMaybe<Scalars['String']['input']>;
  dueDate?: InputMaybe<Scalars['Date']['input']>;
  estimate?: InputMaybe<Scalars['Int']['input']>;
  priority?: Scalars['Int']['input'];
  title: Scalars['String']['input'];
};

export type IssueCreatePayload = {
  __typename?: 'IssueCreatePayload';
  errors: Array<ValidationErrorType>;
  issue?: Maybe<Issue>;
};

export type IssueUpdateInput = {
  assigneeId?: InputMaybe<Scalars['UUID']['input']>;
  description?: InputMaybe<Scalars['String']['input']>;
  dueDate?: InputMaybe<Scalars['Date']['input']>;
  estimate?: InputMaybe<Scalars['Int']['input']>;
  priority?: InputMaybe<Scalars['Int']['input']>;
  title?: InputMaybe<Scalars['String']['input']>;
  workflowStateId?: InputMaybe<Scalars['UUID']['input']>;
};

export type IssueUpdatePayload = {
  __typename?: 'IssueUpdatePayload';
  errors: Array<ValidationErrorType>;
  issue?: Maybe<Issue>;
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

export type Mutation = {
  __typename?: 'Mutation';
  issueArchive: IssueArchivePayload;
  issueCreate: IssueCreatePayload;
  issueUpdate: IssueUpdatePayload;
  login: LoginPayload;
  logout: LogoutPayload;
  register: RegisterPayload;
};


export type MutationIssueArchiveArgs = {
  id: Scalars['UUID']['input'];
};


export type MutationIssueCreateArgs = {
  input: IssueCreateInput;
};


export type MutationIssueUpdateArgs = {
  id: Scalars['UUID']['input'];
  input: IssueUpdateInput;
};


export type MutationLoginArgs = {
  input: LoginInput;
};


export type MutationRegisterArgs = {
  input: RegisterInput;
};

export type PageInfo = {
  __typename?: 'PageInfo';
  endCursor?: Maybe<Scalars['String']['output']>;
  hasNextPage: Scalars['Boolean']['output'];
};

export type Query = {
  __typename?: 'Query';
  issue?: Maybe<Issue>;
  issues: IssueConnection;
  me?: Maybe<User>;
  myWorkspace: WorkspaceMembership;
  myWorkspaces: Array<WorkspaceMembership>;
  /** The teams in a workspace, each with the workflow states its issues can occupy. */
  teams: Array<Team>;
};


export type QueryIssueArgs = {
  id: Scalars['UUID']['input'];
};


export type QueryIssuesArgs = {
  after?: InputMaybe<Scalars['String']['input']>;
  first?: Scalars['Int']['input'];
};


export type QueryMyWorkspaceArgs = {
  slug: Scalars['String']['input'];
};


export type QueryTeamsArgs = {
  workspaceSlug: Scalars['String']['input'];
};

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

export type Team = {
  __typename?: 'Team';
  createdAt: Scalars['DateTime']['output'];
  id: Scalars['UUID']['output'];
  /** The prefix of this team's issue identifiers -- the ENG in ENG-42. Unique within the workspace, and not beyond it. */
  key: Scalars['String']['output'];
  name: Scalars['String']['output'];
  workflowStates: Array<WorkflowState>;
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

export type WorkspaceMembership = {
  __typename?: 'WorkspaceMembership';
  createdAt: Scalars['DateTime']['output'];
  role: WorkspaceRole;
  workspace: Workspace;
};

export type WorkspaceRole =
  | 'ADMIN'
  | 'MEMBER'
  | 'OWNER';
