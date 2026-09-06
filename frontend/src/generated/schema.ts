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

export type CycleUpdateInput = {
  endsAt: Scalars['DateTime']['input'];
  id: Scalars['UUID']['input'];
  name?: InputMaybe<Scalars['String']['input']>;
  number: Scalars['Int']['input'];
  startsAt: Scalars['DateTime']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type GithubDisconnectInput = {
  workspaceSlug: Scalars['String']['input'];
};

export type GithubIntegration = {
  __typename?: 'GithubIntegration';
  accountLogin?: Maybe<Scalars['String']['output']>;
  connectedAt?: Maybe<Scalars['DateTime']['output']>;
  connectedById?: Maybe<Scalars['UUID']['output']>;
  repositories: Array<GithubRepository>;
  status: GithubIntegrationStatus;
};

export type GithubIntegrationStatus =
  | 'CONNECTED'
  | 'DISCONNECTED'
  | 'UNCONFIGURED';

export type GithubRepository = {
  __typename?: 'GithubRepository';
  fullName: Scalars['String']['output'];
  repositoryId: Scalars['ID']['output'];
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

export type IssueClearParentInput = {
  issueId: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
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
  teamId: Scalars['UUID']['input'];
  title: Scalars['String']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type IssueCreatePayload = {
  __typename?: 'IssueCreatePayload';
  errors: Array<ValidationErrorType>;
  issue?: Maybe<Issue>;
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

export type LabelPayload = {
  __typename?: 'LabelPayload';
  errors: Array<ValidationErrorType>;
  label?: Maybe<Label>;
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
  githubDisconnect: GithubIntegration;
  /** Redeem an invitation token as the authenticated caller. */
  invitationAccept: InvitationAcceptPayload;
  /** Invite an email address to the workspace. Returns the raw invitation token once and never again. */
  invitationCreate: InvitationCreatePayload;
  /** Withdraw an invitation. Requires the admin or owner role. */
  invitationRevoke: InvitationRevokePayload;
  issueArchive: IssueArchivePayload;
  issueClearParent: IssueParentPayload;
  issueCreate: IssueCreatePayload;
  issueLabelAttach: IssueLabelPayload;
  issueLabelDetach: IssueLabelPayload;
  issueRelationCreate: IssueRelationCreatePayload;
  issueRelationDelete: IssueRelationDeletePayload;
  issueSetCycle: IssueSetCyclePayload;
  issueSetParent: IssueParentPayload;
  issueSetProject: IssueSetProjectPayload;
  issueUpdate: IssueUpdatePayload;
  labelCreate: LabelPayload;
  labelDelete: LabelDeletePayload;
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
  projectMilestoneCreate: ProjectMilestonePayload;
  projectMilestoneDelete: ProjectMilestoneDeletePayload;
  projectMilestoneUpdate: ProjectMilestonePayload;
  projectTeamAdd: ProjectPayload;
  projectTeamRemove: ProjectPayload;
  projectUpdate: ProjectPayload;
  register: RegisterPayload;
  slackDisconnect: SlackDisconnectPayload;
  /** Create a team, seeded with the default workflow states. Requires the admin or owner role. */
  teamCreate: TeamPayload;
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


export type MutationGithubDisconnectArgs = {
  input: GithubDisconnectInput;
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


export type MutationIssueClearParentArgs = {
  input: IssueClearParentInput;
};


export type MutationIssueCreateArgs = {
  input: IssueCreateInput;
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


export type MutationRegisterArgs = {
  input: RegisterInput;
};


export type MutationSlackDisconnectArgs = {
  input: SlackDisconnectInput;
};


export type MutationTeamCreateArgs = {
  input: TeamCreateInput;
};


export type MutationWorkspaceCreateArgs = {
  input: WorkspaceCreateInput;
};

export type Notification = {
  __typename?: 'Notification';
  actorId?: Maybe<Scalars['UUID']['output']>;
  createdAt: Scalars['DateTime']['output'];
  id: Scalars['UUID']['output'];
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
  | 'COMMENTED';

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

export type PageInfo = {
  __typename?: 'PageInfo';
  endCursor?: Maybe<Scalars['String']['output']>;
  hasNextPage: Scalars['Boolean']['output'];
};

export type Project = {
  __typename?: 'Project';
  createdAt: Scalars['DateTime']['output'];
  description?: Maybe<Scalars['String']['output']>;
  id: Scalars['UUID']['output'];
  leadId?: Maybe<Scalars['UUID']['output']>;
  milestones: Array<ProjectMilestone>;
  name: Scalars['String']['output'];
  state: ProjectState;
  targetDate?: Maybe<Scalars['Date']['output']>;
  teamIds: Array<Scalars['UUID']['output']>;
  updatedAt: Scalars['DateTime']['output'];
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

export type ProjectUpdateInput = {
  description?: InputMaybe<Scalars['String']['input']>;
  id: Scalars['UUID']['input'];
  leadId?: InputMaybe<Scalars['UUID']['input']>;
  name?: InputMaybe<Scalars['String']['input']>;
  state?: InputMaybe<ProjectState>;
  targetDate?: InputMaybe<Scalars['Date']['input']>;
  workspaceSlug: Scalars['String']['input'];
};

export type Query = {
  __typename?: 'Query';
  cycle?: Maybe<Cycle>;
  cycles: Array<Cycle>;
  githubIntegration: GithubIntegration;
  /** Invitations to this workspace that have not been accepted or expired. Admins and owners only. */
  invitations: Array<WorkspaceInvitation>;
  issue?: Maybe<Issue>;
  issues: IssueConnection;
  label?: Maybe<Label>;
  labels: LabelConnection;
  me?: Maybe<User>;
  myWorkspace: WorkspaceMembership;
  myWorkspaces: Array<WorkspaceMembership>;
  notificationUnreadCount: Scalars['Int']['output'];
  notifications: NotificationConnection;
  project?: Maybe<Project>;
  projects: ProjectConnection;
  search: SearchResults;
  slackIntegration: SlackIntegration;
  /** The teams in a workspace, each with the workflow states its issues can occupy. */
  teams: Array<Team>;
  /** Everyone in a workspace, with the role each holds. Members only. */
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


export type QueryGithubIntegrationArgs = {
  workspaceSlug: Scalars['String']['input'];
};


export type QueryInvitationsArgs = {
  workspaceSlug: Scalars['String']['input'];
};


export type QueryIssueArgs = {
  id: Scalars['UUID']['input'];
  workspaceSlug: Scalars['String']['input'];
};


export type QueryIssuesArgs = {
  after?: InputMaybe<Scalars['String']['input']>;
  first?: Scalars['Int']['input'];
  teamId?: InputMaybe<Scalars['UUID']['input']>;
  workspaceSlug: Scalars['String']['input'];
};


export type QueryLabelArgs = {
  id: Scalars['UUID']['input'];
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


export type QuerySearchArgs = {
  first?: Scalars['Int']['input'];
  query: Scalars['String']['input'];
  workspaceSlug: Scalars['String']['input'];
};


export type QuerySlackIntegrationArgs = {
  workspaceSlug: Scalars['String']['input'];
};


export type QueryTeamsArgs = {
  workspaceSlug: Scalars['String']['input'];
};


export type QueryWorkspaceMembersArgs = {
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

export type SearchResults = {
  __typename?: 'SearchResults';
  issues: Array<Issue>;
  projects: Array<Project>;
};

export type SlackDisconnectInput = {
  workspaceSlug: Scalars['String']['input'];
};

export type SlackDisconnectPayload = {
  __typename?: 'SlackDisconnectPayload';
  integration: SlackIntegration;
};

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

export type Team = {
  __typename?: 'Team';
  createdAt: Scalars['DateTime']['output'];
  id: Scalars['UUID']['output'];
  /** The prefix of this team's issue identifiers -- the ENG in ENG-42. Unique within the workspace, and not beyond it. */
  key: Scalars['String']['output'];
  name: Scalars['String']['output'];
  workflowStates: Array<WorkflowState>;
};

export type TeamCreateInput = {
  /** The prefix of this team's issue identifiers -- the ENG in ENG-42. 1-10 uppercase letters and digits, starting with a letter. Unique within the workspace. */
  key: Scalars['String']['input'];
  name: Scalars['String']['input'];
  workspaceSlug: Scalars['String']['input'];
};

export type TeamPayload = {
  __typename?: 'TeamPayload';
  errors: Array<ValidationErrorType>;
  team?: Maybe<Team>;
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
