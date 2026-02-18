from django.db import models
from django.contrib.auth.models import AbstractUser

# ==========================================
# 1. AUTHENTICATION
# ==========================================
class User(AbstractUser):
    email = models.EmailField(unique=True)
    
    class Role(models.TextChoices):
        ADMIN = "ADMIN", "Admin"
        FACULTY = "FACULTY", "Faculty"

    base_role = Role.ADMIN
    role = models.CharField(max_length=50, choices=Role.choices, default=Role.FACULTY)
    is_approved = models.BooleanField(default=False)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username', 'first_name', 'last_name']

    def __str__(self):
        return self.email

# ==========================================
# 2. ACADEMIC DATA
# ==========================================

class Student(models.Model):
    student_id = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=100)
    
    def __str__(self):
        return f"{self.name} ({self.student_id})"

class Subject(models.Model):

    class GradeLevel(models.IntegerChoices):
        KINDER = 0, 'Kinder'
        GRADE_1 = 1, 'Grade 1'
        GRADE_2 = 2, 'Grade 2'
        GRADE_3 = 3, 'Grade 3'
        GRADE_4 = 4, 'Grade 4'
        GRADE_5 = 5, 'Grade 5'
        GRADE_6 = 6, 'Grade 6'
        GRADE_7 = 7, 'Grade 7'
        GRADE_8 = 8, 'Grade 8'
        GRADE_9 = 9, 'Grade 9'
        GRADE_10 = 10, 'Grade 10'
        GRADE_11 = 11, 'Grade 11'
        GRADE_12 = 12, 'Grade 12'

    name = models.CharField(max_length=100)
    code = models.CharField(max_length=20, unique=True)
    description = models.TextField(blank=True, null=True)
    grade_level = models.IntegerField(
        choices=GradeLevel.choices,
        default=GradeLevel.GRADE_6,
        help_text="The grade/year level this subject belongs to."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.code} - {self.name}"

class Classroom(models.Model):
    """
    This represents a specific class being taught.
    It connects a Teacher + Subject + Section Name.
    """
    teacher = models.ForeignKey(User, on_delete=models.CASCADE, limit_choices_to={'role': User.Role.FACULTY})
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE)
    
    section_name = models.CharField(max_length=50) 
    school_year = models.CharField(max_length=20)
    students = models.ManyToManyField(Student, blank=True, related_name='classrooms')

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.subject.code} ({self.section_name})"

# ==========================================
# 3. QUIZZES & GRADING
# ==========================================

class Quiz(models.Model):
    classroom = models.ForeignKey(Classroom, on_delete=models.CASCADE, related_name='quizzes')
    title = models.CharField(max_length=200)
    total_score = models.IntegerField(default=100)
    
    answer_key = models.JSONField(default=dict, blank=True)

    # Item Analysis / Statistics Cache
    mean_score = models.FloatField(default=0.0, blank=True)
    min_score = models.FloatField(default=0.0, blank=True)
    max_score = models.FloatField(default=0.0, blank=True)
    attendees_count = models.IntegerField(default=0, blank=True) # How many took it

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.title} - {self.classroom.section_name}"

    def update_statistics(self):
        """
        Helper method to recalculate stats whenever a new result is added.
        """
        results = self.results.all()
        if results.exists():
            scores = [r.score_obtained for r in results]
            self.attendees_count = len(scores)
            self.max_score = max(scores)
            self.min_score = min(scores)
            self.mean_score = sum(scores) / len(scores)
        else:
            self.mean_score = 0.0
            self.min_score = 0.0
            self.max_score = 0.0
            self.attendees_count = 0
        
        self.save()

class QuizResult(models.Model):
    quiz = models.ForeignKey(Quiz, on_delete=models.CASCADE, related_name='results')
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='quiz_results')
    
    score_obtained = models.DecimalField(max_digits=5, decimal_places=2)
    student_answers = models.JSONField(default=dict, blank=True)
    scanned_image_url = models.URLField(max_length=500, null=True, blank=True)
    date_taken = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['quiz', 'student']

    def __str__(self):
        return f"{self.student.last_name} - {self.quiz.title}: {self.score_obtained}"