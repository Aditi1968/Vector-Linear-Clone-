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
  /** Date with time (isoformat) */
  DateTime: { input: string; output: string; }
  UUID: { input: string; output: string; }
};

export type Issue = {
  __typename?: 'Issue';
  children: IssueSummaryConnection;
  completedAt?: Maybe<Scalars['DateTime']['output']>;
  createdAt: Scalars['DateTime']['output'];
  description?: Maybe<Scalars['String']['output']>;
  id: Scalars['UUID']['output'];
  parent?: Maybe<IssueSummary>;
  priority: Scalars['Int']['output'];
  relations: IssueRelationConnection;
  title: Scalars['String']['output'];
  updatedAt: Scalars['DateTime']['output'];
};


export type IssueChildrenArgs = {
  after?: InputMaybe<Scalars['String']['input']>;
  first?: Scalars['Int']['input'];
};


export type IssueRelationsArgs = {
  after?: InputMaybe<Scalars['String']['input']>;
  first?: Scalars['Int']['input'];
};

export type IssueClearParentInput = {
  issueId: Scalars['UUID']['input'];
};

export type IssueConnection = {
  __typename?: 'IssueConnection';
  nodes: Array<Issue>;
  pageInfo: PageInfo;
};

export type IssueCreateInput = {
  description?: InputMaybe<Scalars['String']['input']>;
  priority?: Scalars['Int']['input'];
  title: Scalars['String']['input'];
};

export type IssueCreatePayload = {
  __typename?: 'IssueCreatePayload';
  errors: Array<ValidationErrorType>;
  issue?: Maybe<Issue>;
};

export type IssueParentPayload = {
  __typename?: 'IssueParentPayload';
  errors: Array<ValidationErrorType>;
  issue?: Maybe<Issue>;
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
};

export type IssueRelationCreatePayload = {
  __typename?: 'IssueRelationCreatePayload';
  errors: Array<ValidationErrorType>;
  relation?: Maybe<IssueRelation>;
};

export type IssueRelationDeleteInput = {
  id: Scalars['UUID']['input'];
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

export type IssueSetParentInput = {
  issueId: Scalars['UUID']['input'];
  parentId: Scalars['UUID']['input'];
};

export type IssueSummary = {
  __typename?: 'IssueSummary';
  completedAt?: Maybe<Scalars['DateTime']['output']>;
  createdAt: Scalars['DateTime']['output'];
  description?: Maybe<Scalars['String']['output']>;
  id: Scalars['UUID']['output'];
  priority: Scalars['Int']['output'];
  title: Scalars['String']['output'];
  updatedAt: Scalars['DateTime']['output'];
};

export type IssueSummaryConnection = {
  __typename?: 'IssueSummaryConnection';
  nodes: Array<IssueSummary>;
  pageInfo: PageInfo;
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
  issueClearParent: IssueParentPayload;
  issueCreate: IssueCreatePayload;
  issueRelationCreate: IssueRelationCreatePayload;
  issueRelationDelete: IssueRelationDeletePayload;
  issueSetParent: IssueParentPayload;
  login: LoginPayload;
  logout: LogoutPayload;
  register: RegisterPayload;
};


export type MutationIssueClearParentArgs = {
  input: IssueClearParentInput;
};


export type MutationIssueCreateArgs = {
  input: IssueCreateInput;
};


export type MutationIssueRelationCreateArgs = {
  input: IssueRelationCreateInput;
};


export type MutationIssueRelationDeleteArgs = {
  input: IssueRelationDeleteInput;
};


export type MutationIssueSetParentArgs = {
  input: IssueSetParentInput;
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
