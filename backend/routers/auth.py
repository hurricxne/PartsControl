from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from pydantic import BaseModel, EmailStr
from database import get_db
from models.models import User
from auth import verify_password, get_password_hash, create_access_token, get_current_user

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: str
    password: str


class RegisterRequest(BaseModel):
    email: str
    nombre: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str
    user: dict


@router.post("/login", response_model=TokenResponse)
def login(data: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == data.email, User.is_active == 1).first()
    if not user or not verify_password(data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email o contraseña incorrectos"
        )
    token = create_access_token({"sub": str(user.id)})
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {"id": user.id, "email": user.email, "nombre": user.nombre, "empresa": user.empresa or "mineria"}
    }


@router.post("/register", status_code=201)
def register(data: RegisterRequest, db: Session = Depends(get_db)):
    if db.query(User).filter(User.email == data.email).first():
        raise HTTPException(status_code=400, detail="El email ya está registrado")
    user = User(
        email=data.email,
        nombre=data.nombre,
        hashed_password=get_password_hash(data.password)
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"message": "Usuario creado", "id": user.id}


@router.get("/me")
def me(current_user: User = Depends(get_current_user)):
    return {"id": current_user.id, "email": current_user.email, "nombre": current_user.nombre, "empresa": current_user.empresa or "mineria"}


@router.get("/users")
def listar_usuarios(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Usuarios activos de la misma empresa — alimenta los selectores de 'asesor responsable'."""
    empresa = current_user.empresa or "mineria"
    usuarios = (
        db.query(User)
        .filter(User.is_active == 1, User.empresa == empresa)
        .order_by(User.nombre)
        .all()
    )
    return [
        {"id": u.id, "nombre": u.nombre, "email": u.email, "empresa": u.empresa or "mineria"}
        for u in usuarios
    ]
