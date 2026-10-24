from rest_framework import filters, generics, mixins, permissions, viewsets
from rest_framework.response import Response
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.decorators import action
from rest_framework_simplejwt.views import TokenObtainPairView

from django.db import transaction
from django.conf import settings
from django.db.models import Count, F, Max, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django_filters.rest_framework import DjangoFilterBackend

from . import answer_keys, scanner_client
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
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        serializer = UserProfileSerializer(request.user)
        return Response(serializer.data)

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
    queryset = User.objects.all()
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
    permission_classes = [IsAuthenticated]
    """
    This handles EVERYTHING: 
    - GET /api/subjects/ (List)
    - POST /api/subjects/ (Create)
    - PUT /api/subjects/5/ (Update)
    - DELETE /api/subjects/5/ (Delete)
    """
    queryset = Subject.objects.all().order_by('code')
    serializer_class = SubjectSerializer
    permission_classes = [IsAuthenticated]

    # Enable filtering
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['grade_level']

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

                # Existing students (matched by name) keep their ID; new ones get
                # the next YYNNNN IDs for this year (see student_ids.py).
                names = []
                for raw in df['name']:
                    name = str(raw).strip()
                    if name and name.lower() != 'nan':
                        names.append(name.title())
                names = list(dict.fromkeys(names))  # de-duplicate, keep order

                with transaction.atomic():
                    existing = {s.name: s for s in Student.objects.filter(name__in=names)}
                    new_names = [n for n in names if n not in existing]
                    try:
                        new_ids = allocate_student_ids(len(new_names))
                    except ValueError as e:
                        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

                    created = Student.objects.bulk_create(
                        [Student(name=n, student_id=sid) for n, sid in zip(new_names, new_ids)]
                    )
                    students_to_create = list(existing.values()) + created

                    if students_to_create:
                        classroom.students.add(*students_to_create)
                
                return Response({
                    "status": "success",
                    "message": f"Successfully processed {len(students_to_create)} students "
                               f"({len(created)} new)."
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
                    }
                )
                saved_count += 1

        quiz.update_statistics()

        return Response({
            "status": "success",
            "saved": saved_count,
            "errors": errors
        })
    
    @action(detail=True, methods=['get'])
    def export_report(self, request, pk=None):
        """
        Exports a detailed CSV report for the quiz.
        Structure:
        - Quiz Title, Class, Date
        - Statistics (Mean, Min, Max)
        - [Blank Line]
        - Item Analysis Header
        - Q1, Q2, ... (Success Rate)
        - [Blank Line]
        - Student Results Header
        - ID, Name, Score, Date Taken
        """
        quiz = self.get_object()
        
        # Create the HttpResponse object with the appropriate CSV header.
        response = HttpResponse(content_type='text/csv')
        filename = f"{quiz.title}_{quiz.classroom.section_name}_Report.csv".replace(' ', '_')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        writer = csv.writer(response)

        # 1. QUIZ INFO & STATS
        writer.writerow(['QUIZ REPORT', quiz.title])
        writer.writerow(['Classroom', quiz.classroom.section_name])
        writer.writerow(['Subject', quiz.classroom.subject.code])
        writer.writerow(['Date Created', quiz.created_at.strftime('%Y-%m-%d')])
        writer.writerow([])
        
        writer.writerow(['STATISTICS'])
        writer.writerow(['Total Students', quiz.attendees_count])
        writer.writerow(['Mean Score', round(quiz.mean_score, 2)])
        writer.writerow(['Highest Score', quiz.max_score])
        writer.writerow(['Lowest Score', quiz.min_score])
        writer.writerow(['Perfect Score', quiz.total_score])
        writer.writerow([])

        # 2. ITEM ANALYSIS
        # Calculate it manually or use the serializer logic if extracted to a helper
        # For efficiency, we'll re-implement a lightweight version here:
        all_results = quiz.results.all()
        correct_counts = {}
        total_respondents = all_results.count()

        if total_respondents > 0:
            for res in all_results:
                answers = res.student_answers or {}
                for q_num, details in answers.items():
                    if q_num not in correct_counts: correct_counts[q_num] = 0
                    if details.get('correct') is True: correct_counts[q_num] += 1
            
            # Sort keys
            sorted_questions = sorted(correct_counts.keys(), key=lambda x: int(x) if x.isdigit() else x)
            
            writer.writerow(['ITEM ANALYSIS (Difficulty Index)'])
            writer.writerow(['Question #', 'Correct Count', 'Success Rate (%)'])
            
            for q in sorted_questions:
                count = correct_counts[q]
                pct = round((count / total_respondents) * 100, 1)
                writer.writerow([q, count, f"{pct}%"])
            writer.writerow([])

        # 3. STUDENT RESULTS
        writer.writerow(['STUDENT RESULTS'])
        writer.writerow(['Student ID', 'Name', 'Score', 'Percentage', 'Date Taken'])

        # Get all students in the class (even those who haven't taken it)
        all_students = quiz.classroom.students.all().order_by('name')
        existing_results = {res.student.id: res for res in all_results}

        for student in all_students:
            result = existing_results.get(student.id)
            if result:
                pct = round((result.score_obtained / quiz.total_score) * 100, 1)
                date_str = result.date_taken.strftime('%Y-%m-%d %H:%M')
                writer.writerow([
                    student.student_id, 
                    student.name, 
                    result.score_obtained, 
                    f"{pct}%", 
                    date_str
                ])
            else:
                writer.writerow([
                    student.student_id, 
                    student.name, 
                    "N/A", 
                    "N/A", 
                    "Not Taken"
                ])

        return response