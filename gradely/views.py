from rest_framework import filters, generics, mixins, permissions, viewsets
from rest_framework.response import Response
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.decorators import action
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.views import TokenObtainPairView

from django.db import transaction
from django.conf import settings
from django.db.models import Count, F, Max, Q
from django.http import FileResponse, HttpResponse
from django.shortcuts import get_object_or_404
from django_filters.rest_framework import DjangoFilterBackend

from . import answer_keys, exports, image_retention, rosters, scanner_client
from .permissions import isSchoolAdmin
from .student_ids import allocate_student_ids
from .models import User, Classroom, Quiz, Subject, Student, QuizResult
from .serializers import (
    UserProfileSerializer,
    FacultySignupSerializer, 
    UserApprovalSerializer, 
    ClassroomSerializer, 
    ClassroomDetailSerializer,
    QuizSerializer, 
    SubjectSerializer,
    StudentSerializer,
    PasswordChangeSerializer,
    StudentUploadSerializer,
    QuizDetailSerializer,
    QuizResultSerializer,
    CustomTokenObtainPairSerializer,
    FacultyAdminSerializer,
    FacultyAdminDetailSerializer,
    SetPasswordSerializer,
    AdminQuizSerializer,
)

import pandas as pd
import csv

class CurrentUserView(APIView):
    """GET your profile; PATCH {first_name, last_name} to edit your name."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        serializer = UserProfileSerializer(request.user)
        return Response(serializer.data)

    def patch(self, request):
        serializer = UserProfileSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class ChangePasswordView(APIView):
    """POST {current_password, new_password}. Forgotten passwords are reset by an admin (Faculty page)."""
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'login'

    def post(self, request):
        serializer = PasswordChangeSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        request.user.set_password(serializer.validated_data['new_password'])
        request.user.save(update_fields=['password'])
        return Response({"message": "Password changed."})

#====================================
#     ADMIN WORKS
#====================================
class AdminDashboardView(APIView):
    permission_classes = [isSchoolAdmin] # Ensure only Admins can see this

    def get(self, request):
        # 1. Basic Counts
        total_students = Student.objects.count()
        total_classes = Classroom.objects.count()
        total_faculty = User.objects.filter(role=User.Role.FACULTY, is_approved=True).count()
        pending_approvals = User.objects.filter(is_approved=False).count()

        # 2. Subjects per Grade Level
        subjects_distribution = Subject.objects.values('grade_level').annotate(
            count=Count('id')
        ).order_by('grade_level')

        # 3. Quiz Statistics (Global View)
        # Fetch recent quizzes with their stats
        recent_quizzes = Quiz.objects.all().order_by('-created_at')[:10]
        quiz_data = QuizSerializer(recent_quizzes, many=True).data

        return Response({
            "stats": {
                "total_students": total_students,
                "total_classes": total_classes,
                "total_faculty": total_faculty,
                "pending_approvals": pending_approvals,
            },
            "subjects_distribution": subjects_distribution,
            "recent_quizzes": quiz_data
        })


class FacultySignupView(generics.CreateAPIView):
    queryset = User.objects.all()
    serializer_class = FacultySignupSerializer
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'signup'

# Admin: Lists faculty users that are not approved
class PendingFacultyListView(generics.ListAPIView):
    queryset = User.objects.filter(
        is_approved=False, 
        role__in=[User.Role.FACULTY, User.Role.ADMIN]
    ).order_by('-date_joined')
    
    serializer_class = UserApprovalSerializer
    permission_classes = [isSchoolAdmin]

# Admin: approve or reject faculty signups
class ApproveFacultyView(generics.UpdateAPIView):
    queryset = User.objects.all()
    serializer_class = UserApprovalSerializer
    permission_classes = [isSchoolAdmin]
    lookup_field = 'pk'

    # OCustom patch method to ensure they only toggle approval
    def patch(self, request, *args, **kwargs):
        return super().patch(request, *args, **kwargs)

class RejectFacultyView(generics.DestroyAPIView):
    # Only sign-ups still waiting for approval; approved accounts are managed on the Faculty page.
    queryset = User.objects.filter(is_approved=False)
    permission_classes = [isSchoolAdmin]
    lookup_field = 'pk'

class AdminFacultyViewSet(viewsets.ModelViewSet):
    """
    Admin-only management of faculty accounts:
      GET    /admin/faculty/               list, with class/student/quiz counts
      POST   /admin/faculty/               create (starts approved)
      GET    /admin/faculty/{id}/          details + their classes
      PATCH  /admin/faculty/{id}/          edit name/email, approve, activate/deactivate
      DELETE /admin/faculty/{id}/          delete the account AND their classes, quizzes, results
      POST   /admin/faculty/{id}/set_password/
    Admin accounts are never listed or touched here.
    """
    permission_classes = [isSchoolAdmin]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['is_active', 'is_approved']

    def get_queryset(self):
        return (User.objects.filter(role=User.Role.FACULTY)
                .annotate(class_count=Count('classroom', distinct=True),
                          student_count=Count('classroom__students', distinct=True),
                          quiz_count=Count('classroom__quizzes', distinct=True))
                .order_by('last_name', 'first_name', 'email'))

    def get_serializer_class(self):
        return FacultyAdminDetailSerializer if self.action == 'retrieve' else FacultyAdminSerializer

    @action(detail=True, methods=['post'])
    def set_password(self, request, pk=None):
        faculty = self.get_object()
        serializer = SetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        faculty.set_password(serializer.validated_data['password'])
        faculty.save(update_fields=['password'])
        return Response({"message": f"Password updated for {faculty.email}."})


class AdminQuizViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """
    Admin-only overview of every quiz in the school, for monitoring how classes are doing.
      GET /admin/quizzes/?classroom__teacher=&classroom__subject__grade_level=&classroom__subject=
                         &classroom__school_year=&search=&ordering=
    Details and per-student results come from GET /quizzes/{id}/ (admins can read any quiz).
    """
    permission_classes = [isSchoolAdmin]
    serializer_class = AdminQuizSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['classroom', 'classroom__teacher', 'classroom__subject',
                        'classroom__subject__grade_level', 'classroom__school_year']
    search_fields = ['title', 'classroom__section_name', 'classroom__subject__code', 'classroom__subject__name',
                     'classroom__teacher__first_name', 'classroom__teacher__last_name']
    ordering_fields = ['created_at', 'mean_score', 'attendees_count', 'title', 'last_scanned']
    ordering = ['-created_at']

    def get_queryset(self):
        passing = settings.PASSING_PERCENT / 100
        return (Quiz.objects.select_related('classroom__subject', 'classroom__teacher')
                .annotate(
                    class_size=Count('classroom__students', distinct=True),
                    passing_count=Count('results', distinct=True,
                                        filter=Q(results__score_obtained__gte=F('total_score') * passing)),
                    last_scanned=Max('results__date_taken'),
                ))


# To get the role on the passed token
class CustomTokenObtainPairView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer
    throttle_classes = [ScopedRateThrottle]   # slows down password guessing
    throttle_scope = 'login'

# ====================================
#    USER PROFILE
# ====================================
class UserViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API endpoint that allows users to be viewed.
    Using ReadOnlyModelViewSet so specific user details can be fetched
    but not deleted/created via this endpoint.
    """
    queryset = User.objects.all()
    serializer_class = UserProfileSerializer # Make sure to import this from serializers.py
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        # Admins can look anyone up; everyone else only sees their own account.
        if self.request.user.role == User.Role.ADMIN:
            return User.objects.all()
        return User.objects.filter(id=self.request.user.id)

#====================================
#     FACULTY PAGES
#====================================
# Faculty: Dashboard page
class TeacherDashboardView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        
        classrooms = (Classroom.objects.filter(teacher=user)
                      .select_related('subject', 'teacher')
                      .annotate(total_students=Count('students'))
                      .order_by('-created_at'))
        quizzes = (Quiz.objects.filter(classroom__teacher=user)
                   .select_related('classroom', 'classroom__subject')
                   .order_by('-created_at')[:5])

        return Response({
            "classes": ClassroomSerializer(classrooms, many=True).data,
            "recent_quizzes": QuizSerializer(quizzes, many=True).data,
            "total_students": Student.objects.filter(classrooms__teacher=user).distinct().count(),
            "total_classes": classrooms.count()
        })
    
# Faculty: Subjects page
class SubjectViewSet(viewsets.ModelViewSet):
    """
    Subjects are shared by every teacher's classes.
      GET, POST            any signed-in user (teachers add the subjects they teach)
      PUT, PATCH, DELETE   admins only; a subject that still has classes can't be deleted
    """
    queryset = Subject.objects.annotate(class_count=Count('classroom')).order_by('code')
    serializer_class = SubjectSerializer

    # Enable filtering
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['grade_level']

    def get_permissions(self):
        if self.action in ('update', 'partial_update', 'destroy'):
            return [isSchoolAdmin()]
        return [IsAuthenticated()]

    def destroy(self, request, *args, **kwargs):
        subject = self.get_object()
        if subject.class_count:
            return Response({"error": f"{subject.code} is used by {subject.class_count} class(es). "
                                      "Delete or move those classes first."},
                            status=status.HTTP_400_BAD_REQUEST)
        return super().destroy(request, *args, **kwargs)

class ClassroomViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]

    # Enable Filtering
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['subject__grade_level']

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return ClassroomDetailSerializer
        return ClassroomSerializer

    def get_queryset(self):
        user = self.request.user

        queryset = Classroom.objects.select_related('subject', 'teacher').annotate(
            total_students=Count('students')
        )

        if user.role == 'ADMIN':
            return queryset.order_by('-created_at')
            
        return queryset.filter(teacher=user).order_by('-created_at')

    def perform_create(self, serializer):
        # Automation: Automatically assign the logged-in user as the teacher
        serializer.save(teacher=self.request.user)

    @action(detail=False, methods=['get'])
    def global_list(self, request):
        """
        Returns a list of ALL classrooms that have at least one student.
        """
        queryset = Classroom.objects.select_related('subject', 'teacher').annotate(
            total_students=Count('students')
        ).filter(
            total_students__gt=0  # <--- NEW: Only show classes with students
        ).order_by('subject__code', 'section_name')
        
        serializer = ClassroomSerializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def copy_list(self, request, pk=None):
        """
        Copy all students from ANY existing classroom to this classroom.
        """
        target_classroom = self.get_object()
        source_id = request.data.get('source_classroom_id')

        if not source_id:
            return Response({"error": "Source classroom ID required"}, status=400)

        # This allows fetching students from any class in the system
        source_classroom = get_object_or_404(Classroom, id=source_id)

        students_to_add = source_classroom.students.all()
        if not students_to_add.exists():
            return Response({"error": "Source classroom has no students."}, status=400)
            
        # Atomic transaction ensures all or nothing are added
        with transaction.atomic():
            target_classroom.students.add(*students_to_add)

        return Response({
            "message": f"Successfully copied {students_to_add.count()} students from {source_classroom.section_name}."
        })

    @action(detail=True, methods=['post'])
    def remove_student(self, request, pk=None):
        """
        Removes a student from the classroom roster.
        Endpoint: POST /api/classrooms/{id}/remove_student/
        Payload: { "student_id": 12 }
        """
        classroom = self.get_object()
        student_id = request.data.get('student_id')

        if not student_id:
            return Response(
                {"error": "Student ID is required."}, 
                status=status.HTTP_400_BAD_REQUEST
            )

        # Get the student object or return 404
        student = get_object_or_404(Student, id=student_id)

        # Check if the student is actually in this class
        if student in classroom.students.all():
            classroom.students.remove(student)
            return Response(
                {"message": f"Student {student.name} removed from class."}, 
                status=status.HTTP_200_OK
            )
        else:
            return Response(
                {"error": "Student is not enrolled in this class."}, 
                status=status.HTTP_400_BAD_REQUEST
            )

    @action(detail=True, methods=['post'])
    def add_student(self, request, pk=None):
        """
        Adds one student without a roster file.
        Payload: {"name": "Juan Dela Cruz"}, plus one of
          "student_pk": 12      enroll this existing student (picked from the 409 candidates)
          "create_new": true    a new student even though others share the name
        Without either, a 409 lists existing students with that name so the teacher can choose.
        """
        classroom = self.get_object()
        if request.data.get('student_pk'):
            student = get_object_or_404(Student, id=request.data['student_pk'])
            created = False
        else:
            name = rosters.clean_name(request.data.get('name'))
            if not name:
                return Response({"error": "Enter the student's name."}, status=status.HTTP_400_BAD_REQUEST)
            matches = rosters.same_name_students(name)
            if matches and not request.data.get('create_new'):
                return Response({
                    "error": f"{len(matches)} student(s) named {name} already exist. "
                             "Choose one, or add a new student.",
                    "candidates": [{
                        "id": s.id, "student_id": s.student_id, "name": s.name,
                        "in_this_class": any(c.id == classroom.id for c in s.classrooms.all()),
                        "classes": [f"{c.subject.code} {c.section_name}" for c in s.classrooms.all()],
                    } for s in matches],
                }, status=status.HTTP_409_CONFLICT)
            with transaction.atomic():
                try:
                    student = Student.objects.create(name=name, student_id=allocate_student_ids(1)[0])
                except ValueError as e:
                    return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
            created = True

        if classroom.students.filter(id=student.id).exists():
            return Response({"error": f"{student.name} is already in this class."}, status=status.HTTP_400_BAD_REQUEST)
        classroom.students.add(student)
        return Response({"student": StudentSerializer(student).data, "created": created},
                        status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['get'], url_path=r'students/(?P<student_pk>\d+)')
    def student_results(self, request, pk=None, student_pk=None):
        """One student's results on every quiz of this class."""
        classroom = self.get_object()
        student = get_object_or_404(classroom.students, id=student_pk)
        results = {r.quiz_id: r for r in QuizResult.objects.filter(student=student, quiz__classroom=classroom)}
        passing = settings.PASSING_PERCENT / 100
        quizzes = []
        for quiz in _quizzes_in_order(classroom):
            result = results.get(quiz.id)
            score = float(result.score_obtained) if result else None
            quizzes.append({
                "quiz_id": quiz.id, "title": quiz.title, "date": quiz.date_given or quiz.created_at.date(),
                "total": quiz.total_score, "score": score,
                "percent": round(score / quiz.total_score * 100, 1) if result else None,
                "passed": score >= quiz.total_score * passing if result else None,
                "class_mean": round(quiz.mean_score, 1) if quiz.attendees_count else None,
                "has_image": bool(result and result.scanned_image_url),
                "entered_manually": bool(result and result.entered_manually),
            })
        taken = [q["percent"] for q in quizzes if q["percent"] is not None]
        return Response({
            "student": StudentSerializer(student).data,
            "classroom": {"id": classroom.id, "section_name": classroom.section_name,
                          "subject_code": classroom.subject.code, "subject_name": classroom.subject.name,
                          "school_year": classroom.school_year},
            "quizzes": quizzes,
            "average_percent": round(sum(taken) / len(taken), 1) if taken else None,
            "taken": len(taken),
        })

    @action(detail=True, methods=['get'])
    def gradebook(self, request, pk=None):
        """CSV: every student in the class against every quiz, with each student's average %."""
        classroom = self.get_object()
        quizzes = _quizzes_in_order(classroom)
        scores = {(r.student_id, r.quiz_id): r.score_obtained
                  for r in QuizResult.objects.filter(quiz__classroom=classroom)}

        response = HttpResponse(content_type='text/csv; charset=utf-8')
        filename = f"{classroom.subject.code}_{classroom.section_name}_Gradebook.csv".replace(' ', '_')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        response.write('\ufeff')   # BOM so Excel reads names like "Peña" correctly
        writer = csv.writer(response)
        writer.writerow(['GRADEBOOK', f"{classroom.subject.code} {classroom.subject.name}",
                         classroom.section_name, classroom.school_year])
        writer.writerow([])
        writer.writerow(['Student ID', 'Name'] + [q.title for q in quizzes] + ['Quizzes Taken', 'Average %'])
        writer.writerow(['', 'Date'] + [(q.date_given or q.created_at.date()).isoformat() for q in quizzes] + ['', ''])
        writer.writerow(['', 'Items'] + [q.total_score for q in quizzes] + ['', ''])
        for student in classroom.students.order_by('name'):
            row, percents = [], []
            for quiz in quizzes:
                score = scores.get((student.id, quiz.id))
                if score is None:
                    row.append('')
                else:
                    row.append(int(score) if score == int(score) else float(score))
                    percents.append(float(score) / quiz.total_score * 100)
            average = f"{round(sum(percents) / len(percents), 1)}%" if percents else ''
            writer.writerow([student.student_id, student.name] + row + [len(percents), average])
        return response


def _quizzes_in_order(classroom):
    return sorted(classroom.quizzes.all(), key=lambda q: (q.date_given or q.created_at.date(), q.created_at))


class StudentViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.UpdateModelMixin,
                     viewsets.GenericViewSet):
    """
    Students are created by roster import and removed from a class with
    ClassroomViewSet.remove_student, so this only lists and edits them.
    Teachers see only students enrolled in their own classes.
    """
    queryset = Student.objects.all()
    serializer_class = StudentSerializer

    def get_queryset(self):
        user = self.request.user
        queryset = Student.objects.order_by('name')
        if user.role == User.Role.ADMIN:
            return queryset
        return queryset.filter(classrooms__teacher=user).distinct()

class UploadStudentsView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, classroom_id, format=None):
        serializer = StudentUploadSerializer(data=request.data)
        
        if serializer.is_valid():
            uploaded_file = request.FILES['file']
            filename = uploaded_file.name.lower() # Normalize to lowercase for checking
            
            try:
                # 1. Fetch Classroom
                classroom = Classroom.objects.get(id=classroom_id, teacher=request.user)

                # 2. CONDITIONAL PARSING: Check extension
                if filename.endswith('.csv'):
                    df = pd.read_csv(uploaded_file)
                elif filename.endswith(('.xls', '.xlsx')):
                    # 'engine' is usually auto-detected, but explicit is safer
                    df = pd.read_excel(uploaded_file) 
                else:
                    return Response(
                        {"error": "Unsupported file format. Please upload .csv or .xlsx"}, 
                        status=status.HTTP_400_BAD_REQUEST
                    )

                # 3. Validation: Ensure 'name' column exists
                # We normalize column names to lowercase to be forgiving (e.g. "Name" -> "name")
                df.columns = df.columns.str.lower().str.strip()
                
                if 'name' not in df.columns:
                    return Response(
                        {"error": "File must contain a 'name' column."}, 
                        status=status.HTTP_400_BAD_REQUEST
                    )

                # Optional student_id column: pins a row to that exact student (see rosters.py).
                id_column = next((c for c in ('student_id', 'student id', 'id') if c in df.columns), None)
                rows = []
                for index, record in df.iterrows():
                    name = rosters.clean_name(record['name'])
                    if name:
                        sid = rosters.clean_id(record[id_column]) if id_column else ''
                        rows.append((index + 2, name, sid))   # +2: header row and 1-based numbering

                with transaction.atomic():
                    try:
                        students, created_count, warnings = rosters.resolve_rows(classroom, rows)
                    except ValueError as e:
                        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
                    if students:
                        classroom.students.add(*students)

                return Response({
                    "status": "success",
                    "message": f"Successfully processed {len(students)} students ({created_count} new).",
                    "warnings": warnings,
                }, status=status.HTTP_200_OK)

            except Classroom.DoesNotExist:
                return Response({"error": "Classroom not found"}, status=status.HTTP_404_NOT_FOUND)
            except Exception as e:
                return Response({"error": f"Error processing file: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)
        
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

class ScannerStatusView(APIView):
    """GET: is the scanner service awake? The frontend polls this before scanning."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(scanner_client.health())


def _grade_answers(student_answers, key):
    """
    student_answers: {"1": {"selected": "A", ...}} as returned by the scanner (possibly edited).
    Returns (score, answers) where every item of the key has selected/key/correct/needs_review.
    """
    score = 0
    graded = {}
    for item, correct_answer in key.items():
        entry = student_answers.get(item) or {}
        selected = str(entry.get('selected') or '-').strip().upper()
        is_correct = selected == correct_answer
        score += is_correct
        graded[item] = {
            "selected": selected,
            "key": correct_answer,
            "correct": is_correct,
            "needs_review": bool(entry.get('needs_review', False)),
        }
    return score, graded


class EntryError(ValueError):
    pass


def _entry_from_request(quiz, data):
    """
    A teacher-typed result: {"answers": {"1": "A", ...}} (graded against the key) or
    {"score": 17} when only the score is known. Returns (score, student_answers).
    """
    if data.get('answers') is not None:
        key = quiz.answer_key or {}
        if len(key) != quiz.total_score:
            raise EntryError("Set the complete answer key before entering answers.")
        answers = data['answers']
        if not isinstance(answers, dict):
            raise EntryError("answers must map item numbers to letters.")
        allowed = set(answer_keys.CHOICES) | {'-', ''}
        for item, letter in answers.items():
            if str(letter or '').strip().upper() not in allowed:
                raise EntryError(f"Item {item}: answer must be one of {', '.join(answer_keys.CHOICES)} or blank.")
        return _grade_answers({str(i): {"selected": a} for i, a in answers.items()}, key)

    try:
        score = float(data.get('score'))
    except (TypeError, ValueError):
        raise EntryError("Enter the answers or a score.")
    if not 0 <= score <= quiz.total_score or score != int(score):
        raise EntryError(f"Score must be a whole number from 0 to {quiz.total_score}.")
    return int(score), {}


def _regrade_results(quiz):
    """Re-scores saved results after the answer key changes. Returns how many were updated."""
    results = list(quiz.results.all())
    for result in results:
        result.score_obtained, result.student_answers = _grade_answers(result.student_answers or {}, quiz.answer_key)
    QuizResult.objects.bulk_update(results, ['score_obtained', 'student_answers'])
    return len(results)


class QuizViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = QuizSerializer

    # Enable filtering so we can fetch quizzes for a specific classroom
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['classroom']

    def get_queryset(self):
        user = self.request.user

        # Optimize: Fetch Classroom AND the Subject inside that Classroom
        queryset = Quiz.objects.select_related('classroom', 'classroom__subject')

        if user.role == 'ADMIN':
            return queryset.order_by('-created_at')

        return queryset.filter(classroom__teacher=user).order_by('-created_at')

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return QuizDetailSerializer
        return QuizSerializer

    # ------------------------------------------------------------------
    # Scanning workflow:
    #   1. PUT  answer_key/    teacher sets the key (CSV/Excel upload or JSON)
    #   2. GET  answer_sheet/  printable PDF from the scanner, pre-filled with quiz info
    #   3. POST scan/          photos -> scanner -> results matched to the class roster
    #   4. POST save_results/  teacher-reviewed results are graded and stored
    # ------------------------------------------------------------------

    @action(detail=True, methods=['get', 'put'], url_path='answer_key')
    def answer_key(self, request, pk=None):
        quiz = self.get_object()

        if request.method == 'PUT':
            try:
                if 'file' in request.FILES:
                    key = answer_keys.parse_file(request.FILES['file'], quiz.total_score)
                else:
                    key = answer_keys.normalize(request.data.get('answer_key'), quiz.total_score)
            except answer_keys.AnswerKeyError as e:
                return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

            with transaction.atomic():
                quiz.answer_key = key
                quiz.save(update_fields=['answer_key'])
                regraded = _regrade_results(quiz)
            quiz.update_statistics()
            return Response({"answer_key": key, "num_items": quiz.total_score, "regraded_results": regraded})

        return Response({
            "answer_key": quiz.answer_key,
            "num_items": quiz.total_score,
            "complete": len(quiz.answer_key or {}) == quiz.total_score,
        })

    @action(detail=True, methods=['get'])
    def answer_sheet(self, request, pk=None):
        quiz = self.get_object()
        classroom = quiz.classroom
        teacher = classroom.teacher
        try:
            pdf = scanner_client.generate_answer_sheet(
                num_items=quiz.total_score,
                title=quiz.title,
                subject=classroom.subject.name,
                teacher=f"{teacher.first_name} {teacher.last_name}".strip(),
                grade_section=f"{classroom.subject.get_grade_level_display()} - {classroom.section_name}",
            )
        except scanner_client.ScannerError as e:
            return Response({"error": str(e)}, status=status.HTTP_502_BAD_GATEWAY)

        response = HttpResponse(pdf, content_type='application/pdf')
        filename = f"{quiz.title}_answer_sheet.pdf".replace(' ', '_')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    @action(detail=True, methods=['post'], parser_classes=[MultiPartParser, FormParser])
    def scan(self, request, pk=None):
        """
        Sends photos to the scanner and returns its results without saving them,
        so the teacher can review (and fix) them first. Each sheet gets a
        `student` entry when its shaded ID matches someone in this class.
        """
        quiz = self.get_object()
        files = request.FILES.getlist('files')
        if not files:
            return Response({"error": "Upload at least one photo."}, status=status.HTTP_400_BAD_REQUEST)
        if len(quiz.answer_key or {}) != quiz.total_score:
            return Response({"error": "Set the complete answer key before scanning."},
                            status=status.HTTP_400_BAD_REQUEST)

        try:
            data = scanner_client.scan_sheets(files, quiz.answer_key)
        except scanner_client.ScannerError as e:
            return Response({"error": str(e)}, status=status.HTTP_502_BAD_GATEWAY)
        image_retention.maybe_purge_in_background()   # tidy up old sheet images now and then

        roster = {s.student_id: s for s in quiz.classroom.students.all()}
        saved_ids = set(quiz.results.values_list('student_id', flat=True))
        for result in data.get('results', []):
            if 'error' in result:
                continue
            student = roster.get(result.get('student_id'))
            result['student'] = (
                {"id": student.id, "student_id": student.student_id, "name": student.name}
                if student else None
            )
            result['already_saved'] = bool(student and student.id in saved_ids)
            if not student:
                result.setdefault('warnings', []).append(
                    f"ID {result.get('student_id')} is not in this class. Choose the student manually."
                )
        return Response(data)

    @action(detail=True, methods=['post'])
    def save_results(self, request, pk=None):
        """
        Payload: {"results": [{"student_id": "260001", "student_answers": {...}, "scanned_image_url": "..."}]}
        Scores are always recomputed here from the stored answer key, so a teacher's
        corrections to `selected` during review are graded consistently.
        """
        quiz = self.get_object()
        results_data = request.data.get('results', [])
        key = quiz.answer_key or {}
        if len(key) != quiz.total_score:
            return Response({"error": "Set the complete answer key before saving results."},
                            status=status.HTTP_400_BAD_REQUEST)

        roster = {s.student_id: s for s in quiz.classroom.students.all()}
        saved_count = 0
        errors = []

        with transaction.atomic():
            for item in results_data:
                sid = str(item.get('student_id') or '').strip()
                if not sid:
                    errors.append("A result has no student ID.")
                    continue
                student = roster.get(sid)
                if not student:
                    errors.append(f"Student ID '{sid}' is not enrolled in this class.")
                    continue

                score, answers = _grade_answers(item.get('student_answers') or {}, key)
                QuizResult.objects.update_or_create(
                    quiz=quiz,
                    student=student,
                    defaults={
                        'score_obtained': score,
                        'student_answers': answers,
                        'scanned_image_url': item.get('scanned_image_url'),
                        'entered_manually': False,
                    }
                )
                saved_count += 1

        quiz.update_statistics()

        return Response({
            "status": "success",
            "saved": saved_count,
            "errors": errors
        })
    
    @action(detail=True, methods=['post'])
    def manual_result(self, request, pk=None):
        """
        Saves a result typed in by the teacher, for a sheet that can't be scanned.
        Payload: {"student_pk": 12, "answers": {"1": "A", ...}} or {"student_pk": 12, "score": 17}.
        """
        quiz = self.get_object()
        student = get_object_or_404(quiz.classroom.students, id=request.data.get('student_pk'))
        try:
            score, answers = _entry_from_request(quiz, request.data)
        except EntryError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        result, _ = QuizResult.objects.update_or_create(
            quiz=quiz, student=student,
            defaults={'score_obtained': score, 'student_answers': answers,
                      'scanned_image_url': None, 'entered_manually': True},
        )
        quiz.update_statistics()
        return Response(QuizResultSerializer(result).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['patch', 'delete'], url_path=r'results/(?P<result_id>\d+)')
    def result(self, request, pk=None, result_id=None):
        """
        PATCH {"answers": {...}} or {"score": n}: correct a saved result (its graded image is kept).
        DELETE: remove a wrong result, e.g. a sheet saved under the wrong student.
        """
        quiz = self.get_object()
        result = get_object_or_404(quiz.results, id=result_id)
        if request.method == 'DELETE':
            result.delete()
            quiz.update_statistics()
            return Response(status=status.HTTP_204_NO_CONTENT)

        try:
            result.score_obtained, result.student_answers = _entry_from_request(quiz, request.data)
        except EntryError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        result.save(update_fields=['score_obtained', 'student_answers'])
        quiz.update_statistics()
        return Response(QuizResultSerializer(result).data)

    @action(detail=True, methods=['get'])
    def export_report(self, request, pk=None):
        """CSV report: quiz info, statistics, item analysis and every student's score (see exports.py)."""
        quiz = self.get_object()
        response = HttpResponse(content_type='text/csv')
        filename = f"{quiz.title}_{quiz.classroom.section_name}_Report.csv".replace(' ', '_')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        exports.write_report(quiz, csv.writer(response))
        return response

    @action(detail=True, methods=['get'])
    def export_results(self, request, pk=None):
        """
        ZIP for the teacher to keep: report.csv, answers.csv (each student's letters) and
        graded_sheets/ with every checked answer sheet that is still in storage.
        Headers X-Images-Included / X-Images-Missing tell the frontend how many made it.
        """
        quiz = self.get_object()
        if not quiz.results.exists():
            return Response({"error": "There are no saved results to download yet."},
                            status=status.HTTP_400_BAD_REQUEST)
        archive, summary = exports.build_results_zip(quiz)
        response = FileResponse(archive, as_attachment=True, filename=exports.zip_filename(quiz),
                                content_type='application/zip')
        response['X-Images-Included'] = str(summary['images'])
        response['X-Images-Missing'] = str(len(summary['missing_images']))
        return response
