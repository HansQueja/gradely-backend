from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    CustomTokenObtainPairView,
    AdminDashboardView,
    UserViewSet,
    CurrentUserView,
    FacultySignupView, 
    PendingFacultyListView, 
    ApproveFacultyView,
    RejectFacultyView,
    TeacherDashboardView,
    SubjectViewSet,
    ClassroomViewSet,
    StudentViewSet,
    QuizViewSet,
    ScannerStatusView,
    AdminFacultyViewSet,
    AdminQuizViewSet,
    UploadStudentsView
)

from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
)

# Create a router
router = DefaultRouter()
router.register(r'subjects', SubjectViewSet)
router.register(r'classrooms', ClassroomViewSet, basename='classroom')
router.register(r'students', StudentViewSet)
router.register(r'quizzes', QuizViewSet, basename='quiz')
router.register(r'users', UserViewSet)
router.register(r'admin/faculty', AdminFacultyViewSet, basename='admin-faculty')
router.register(r'admin/quizzes', AdminQuizViewSet, basename='admin-quizzes')

urlpatterns = [
    # Authentication
    path('login/', CustomTokenObtainPairView.as_view(), name='token-obtain-pair'),
    path('token/refresh/', TokenRefreshView.as_view(), name='token-refresh'),

    # Public
    path('signup/', FacultySignupView.as_view(), name='signup'),

    # Faculty pages
    path('dashboard/', TeacherDashboardView.as_view(), name='teacher-dashboard'),

    # All the other REST urls
    path('', include(router.urls)),

    # Helper APIs
    path('classrooms/<int:classroom_id>/upload-students/', UploadStudentsView.as_view(), name='upload-list'),
    path('auth/users/me/', CurrentUserView.as_view(), name='current-user'),
    path('scanner/status/', ScannerStatusView.as_view(), name='scanner-status'),

    # Admin only
    path('admin/dashboard/', AdminDashboardView.as_view(), name='admin-dashboard'),
    path('admin/pending/', PendingFacultyListView.as_view(), name='pending-list'),
    path('admin/approve/<int:pk>/', ApproveFacultyView.as_view(), name='approve-user'),
    path('admin/reject/<int:pk>/', RejectFacultyView.as_view(), name='reject-user'),
]