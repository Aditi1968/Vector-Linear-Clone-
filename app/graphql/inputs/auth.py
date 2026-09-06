import strawberry


@strawberry.input
class RegisterInput:
    email: str
    password: str
    name: str | None = None


@strawberry.input
class LoginInput:
    email: str
    password: str
